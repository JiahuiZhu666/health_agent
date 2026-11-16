"""Bounded public visitor workspaces; official inputs are shared read-only."""
from collections import OrderedDict
from pathlib import Path
import re
import threading
from types import SimpleNamespace

from .conversation import ConversationService
from .jobs import ChatJobs, ConversationBusy


class VisitorWorkspaces:
    def __init__(self, agent, *, maximum=128, concurrent=4):
        self.agent = agent
        self.directory = Path(agent.root) / 'visitors'
        if self.directory.is_symlink():
            raise ValueError('The visitor state directory must not be a symbolic link.')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self._maximum = maximum
        self._capacity = threading.BoundedSemaphore(concurrent)
        self._lock = threading.Lock()
        self._items = OrderedDict()

    def _root(self, visitor):
        if not isinstance(visitor, str) or not re.fullmatch(r'[a-f0-9]{32}', visitor):
            raise ValueError('The visitor session is invalid.')
        root = self.directory / visitor
        for path in (self.directory, root, *(root/name for name in ('runs', 'conversations', '.cache'))):
            if path.is_symlink():
                raise ValueError('Visitor state must not contain symbolic links.')
        return root

    def acquire(self, visitor, *, create=False):
        """Lease an existing workspace; only admitted chat creation makes one."""
        with self._lock:
            root = self._root(visitor)
            if not root.is_dir() and not create:
                raise FileNotFoundError('This conversation or analysis does not exist.')
            item = self._items.get(visitor)
            if item is None:
                if len(self._items) >= self._maximum:
                    for key, candidate in self._items.items():
                        if not candidate.leases and not candidate.jobs.has_active():
                            del self._items[key]
                            break
                    else:
                        raise ConversationBusy('The service is busy. Try again shortly.')
                root.mkdir(mode=0o700, exist_ok=True)
                scoped = self.agent.workspace(root)
                conversations = ConversationService(scoped)
                item = SimpleNamespace(agent=scoped, conversations=conversations,
                                       jobs=ChatJobs(conversations, capacity=self._capacity), leases=0)
                self._items[visitor] = item
            item.leases += 1
            self._items.move_to_end(visitor)
            return item

    def release(self, visitor):
        with self._lock:
            self._items[visitor].leases -= 1
