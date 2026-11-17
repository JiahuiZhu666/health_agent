"""Functional model of the container's startup UID/capability transitions.

Real temporary directories exercise prepare_state's actual path operations.
Only identity, inode ownership, and Linux capability permission checks are
modeled: no real chown/setuid, container, listener, environment file or model
service is used. This complements, rather than replaces, the Linux smoke job.
"""
import errno
from pathlib import Path
import os
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy import entrypoint


class StatePermissions:
    """Owner/search/chmod checks for the private mounted-state subtree only.

    Fixture ancestors stand in for searchable /var/lib; their host temporary
    directory UID/mode is intentionally not the simulated container identity.
    """
    def __init__(self, state, capabilities, *, existing):
        self.state = state
        self.uid = self.gid = 0
        self.groups = [0]
        self.capabilities = set(capabilities)
        self.allowed = {state, *(state / name for name in entrypoint.STATE_SUBDIRECTORIES)}
        self.owners = {path: (10001, 10001) for path in self.allowed} if existing else {}
        self.modes = {path: 0o700 for path in self.allowed} if existing else {}
        self.events = []
        self.mkdir = Path.mkdir
        self.chmod = Path.chmod
        self.is_symlink = Path.is_symlink

    def _path(self, path):
        return Path(path)

    def _search(self, path):
        parent = self._path(path).parent
        while parent == self.state or self.state in parent.parents:
            owner, group = self.owners.get(parent, (0, 0))
            mode = self.modes.get(parent, 0o700)
            execute = 0o100 if self.uid == owner else 0o010 if self.gid == group or group in self.groups else 0o001
            if not mode & execute and not self.capabilities.intersection({'DAC_OVERRIDE', 'DAC_READ_SEARCH'}):
                raise PermissionError(errno.EACCES, 'Simulated directory search denied', str(parent))
            parent = parent.parent

    def _write(self, operation, path):
        path = self._path(path)
        if path not in self.allowed:
            raise AssertionError('Startup must not mutate outside its shallow state directory allowlist.')
        self.events.append((operation, path))
        return path

    def make_directory(self, path, mode=0o777, parents=False, exist_ok=False):
        path = self._path(path); self._search(path)
        existed = path.exists()
        self.mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)
        if not existed:
            self._write('mkdir', path)
            self.owners[path] = (self.uid, self.gid)
            self.modes[path] = path.stat().st_mode & 0o777

    def symbolic_link(self, path):
        self._search(path)
        return self.is_symlink(path)

    def change_owner(self, path, uid, gid):
        path = self._path(path); self._search(path)
        if 'CHOWN' not in self.capabilities:
            raise PermissionError(errno.EPERM, 'Simulated CAP_CHOWN required', str(path))
        self._write('chown', path)
        self.owners[path] = (uid, gid)

    def change_mode(self, path, mode, **kwargs):
        path = self._path(path); self._search(path)
        if self.uid != self.owners[path][0] and 'FOWNER' not in self.capabilities:
            raise PermissionError(errno.EPERM, 'Simulated CAP_FOWNER required after ownership handoff', str(path))
        self._write('chmod', path)
        self.chmod(path, mode, **kwargs)
        self.modes[path] = mode

    def set_groups(self, groups):
        if 'SETGID' not in self.capabilities:
            raise PermissionError(errno.EPERM, 'Simulated CAP_SETGID required')
        self.groups = list(groups); self.events.append(('setgroups', tuple(groups)))

    def set_gid(self, gid):
        if 'SETGID' not in self.capabilities:
            raise PermissionError(errno.EPERM, 'Simulated CAP_SETGID required')
        self.gid = gid; self.events.append(('setgid', gid))

    def set_uid(self, uid):
        if 'SETUID' not in self.capabilities:
            raise PermissionError(errno.EPERM, 'Simulated CAP_SETUID required')
        self.uid = uid
        # Linux clears effective/permitted capabilities when all root UIDs
        # become nonzero, absent keep-caps/securebits (not used by entrypoint).
        self.capabilities.clear(); self.events.append(('setuid', uid))

    def access(self, path, flags):
        path = self._path(path); self._search(path)
        owner, group = self.owners[path]
        mode = self.modes[path]
        shift = 6 if self.uid == owner else 3 if self.gid == group or group in self.groups else 0
        permissions = (mode >> shift) & 7
        return ((not flags & os.W_OK or bool(permissions & 2))
                and (not flags & os.X_OK or bool(permissions & 1)))


class EntrypointCapabilityTests(unittest.TestCase):
    def workflow_capabilities(self):
        text = (entrypoint.CODE_ROOT / '.github/workflows/checks.yml').read_text(encoding='utf-8')
        start = text.index('name: Start resource-bounded container')
        end = text.index('\n      - name:', start + 1)
        return set(re.findall(r'--cap-add(?:=|\s+)([A-Z_]+)', text[start:end]))

    def exercise(self, capabilities, *, existing, expected_error=None):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve(); state = root / 'state'
            outside = root / 'outside.txt'; outside.write_text('OUTSIDE_SENTINEL')
            nested = None
            if existing:
                state.mkdir(mode=0o700)
                for name in entrypoint.STATE_SUBDIRECTORIES:
                    (state / name).mkdir(mode=0o700)
                nested = state / 'data' / 'nested'
                nested.mkdir(mode=0o750)
                (nested / 'keep.txt').write_text('NESTED_SENTINEL')
                (nested / 'keep.txt').chmod(0o640)
            model = StatePermissions(state, capabilities, existing=existing)
            account = SimpleNamespace(pw_uid=10001, pw_gid=10001)

            def mkdir(path, *args, **kwargs): return model.make_directory(path, *args, **kwargs)
            def chmod(path, *args, **kwargs): return model.change_mode(path, *args, **kwargs)
            def is_symlink(path): return model.symbolic_link(path)

            with patch.object(entrypoint.os, 'geteuid', side_effect=lambda: model.uid), \
                    patch.object(entrypoint.pwd, 'getpwnam', return_value=account), \
                    patch.object(entrypoint.os, 'chown', side_effect=model.change_owner), \
                    patch.object(entrypoint.os, 'setgroups', side_effect=model.set_groups), \
                    patch.object(entrypoint.os, 'setgid', side_effect=model.set_gid), \
                    patch.object(entrypoint.os, 'setuid', side_effect=model.set_uid), \
                    patch.object(entrypoint.os, 'access', side_effect=model.access), \
                    patch.object(Path, 'mkdir', mkdir), patch.object(Path, 'chmod', chmod), \
                    patch.object(Path, 'is_symlink', is_symlink):
                if expected_error is not None:
                    with self.assertRaises(PermissionError) as caught:
                        entrypoint.prepare_state(state)
                    self.assertEqual(caught.exception.errno, expected_error)
                    self.assertEqual(model.uid, 0)
                else:
                    self.assertEqual(entrypoint.prepare_state(state), state)
                    self.assertEqual((model.uid, model.gid, model.groups), (10001, 10001, []))
                    self.assertEqual(model.capabilities, set())
                    for path in model.allowed:
                        self.assertEqual(model.owners[path], (10001, 10001))
                        self.assertEqual(model.modes[path], 0o700)
            mutations = [path for operation, path in model.events if operation in ('mkdir', 'chmod', 'chown')]
            self.assertTrue(set(mutations).issubset(model.allowed))
            self.assertEqual(outside.read_text(), 'OUTSIDE_SENTINEL')
            if nested:
                self.assertEqual((nested / 'keep.txt').read_text(), 'NESTED_SENTINEL')
                self.assertEqual(nested.stat().st_mode & 0o777, 0o750)
                self.assertEqual((nested / 'keep.txt').stat().st_mode & 0o777, 0o640)

    def test_declared_ci_capabilities_prepare_a_fresh_root_owned_mount(self):
        self.exercise(self.workflow_capabilities(), existing=False)

    def test_declared_ci_capabilities_prepare_an_existing_private_app_owned_volume(self):
        self.exercise(self.workflow_capabilities(), existing=True)

    def test_old_three_capability_profile_fails_chmod_after_fresh_mount_handoff(self):
        self.exercise({'CHOWN', 'SETUID', 'SETGID'}, existing=False, expected_error=errno.EPERM)

    def test_old_three_capability_profile_cannot_search_an_existing_private_volume(self):
        self.exercise({'CHOWN', 'SETUID', 'SETGID'}, existing=True, expected_error=errno.EACCES)


if __name__ == '__main__':
    unittest.main()
