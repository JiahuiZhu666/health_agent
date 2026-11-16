"""Local NHIS research workspace; all result values come from the shared engine."""
from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime
from pathlib import Path

import streamlit as st
from nhis_agent.engine import Agent, csv_report, markdown_report
from nhis_agent.conversation import ConversationService
from server import load_environment

load_environment()
ROOT = Path(__file__).resolve().parent
PAGES = ['Chat', 'Analysis history', 'Data and methods']
CATEGORY_LABELS = {
    'overall': 'All adults', 'less_than_high_school': 'Less than high school',
    'high_school': 'High school or GED', 'some_college': 'Some college or associate degree',
    'bachelors_plus': 'Bachelor’s degree or higher', 'female': 'Female', 'male': 'Male',
    'hispanic': 'Hispanic, any race', 'nh_white': 'Non-Hispanic White only',
    'nh_black': 'Non-Hispanic Black only', 'nh_asian': 'Non-Hispanic Asian only',
    'nh_other': 'Non-Hispanic other or multiple race',
    '25_34': '25–34 years', '35_44': '35–44 years', '45_54': '45–54 years', '55_64': '55–64 years',
    'below_100': 'Below 100% of the poverty threshold', '100_199': '100% to <200% of the poverty threshold',
    '200_399': '200% to <400% of the poverty threshold', '400_plus': '400%+ of the poverty threshold',
}
st.set_page_config(page_title='NHIS Lab · Chat and research', page_icon='◧', layout='wide', initial_sidebar_state='expanded')
st.markdown('<style>' + (ROOT / 'ui' / 'streamlit.css').read_text(encoding='utf-8') + '</style>', unsafe_allow_html=True)


def esc(value):
    return html.escape(str(value))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def pct(value):
    return f'{value * 100:.1f}%' if finite(value) and 0 <= value <= 1 else '—'


def pp(value):
    return f'{value * 100:+.2f}' if finite(value) else '—'


def _category_name(group_id):
    if group_id in CATEGORY_LABELS:
        return CATEGORY_LABELS[group_id]
    parts = re.split(r'\||__', str(group_id or ''))
    if len(parts) == 2 and all(part in CATEGORY_LABELS for part in parts):
        return ' × '.join(CATEGORY_LABELS[part] for part in parts)
    return None


def _row_label(row):
    target, reference = _category_name(row.get('group_id')), _category_name(row.get('reference_id'))
    if target and reference:
        return target + ' − ' + reference
    return target or str(row.get('label') or row.get('group_id') or 'Unnamed group')


def _process_label(event):
    labels = {'understand': 'Understand the request', 'scope': 'Confirm research scope',
              'data': 'Check real data', 'statistics': 'Calculate survey estimates',
              'validation': 'Check result reliability', 'model': 'Independent model benchmark',
              'report': 'Save evidence and report', 'reply': 'Prepare reply',
              'explain': 'Explain existing results', 'analysis': 'Run the analysis',
              'reply_api': 'Generate reply', 'answer': 'Prepare the answer'}
    label = str(event.get('label') or '')
    return labels.get(event.get('stage'), 'Execution step') if not label or re.search(r'[\u3400-\u9fff]', label) else label


def _html(markup):
    st.markdown(markup, unsafe_allow_html=True)


def _section(title, caption=None):
    _html('<div class="nh-section"><h3>' + esc(title) + '</h3>' + ('<p>' + esc(caption) + '</p>' if caption else '') + '</div>')


def _page_head(title, subtitle):
    _html('<div class="nh-page-head"><h1>' + esc(title) + '</h1><p>' + esc(subtitle) + '</p></div>')


def _table(rows):
    """Static table: first column is the row label, the rest align as figures."""
    if not rows:
        return
    head = ''.join('<th>' + esc(column) + '</th>' for column in rows[0])
    body = ''.join('<tr>' + ''.join('<td>' + esc('—' if value is None else value) + '</td>' for value in row.values()) + '</tr>' for row in rows)
    _html('<div class="nh-table"><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>')


def _choose_example(question):
    st.session_state['chat_draft'] = question


def _keep_settings():
    """Widget state is dropped when its page is not shown; keep our own copy."""
    st.session_state.repeats = int(st.session_state.measure_repeats)
    st.session_state.versions = int(st.session_state.measure_versions)


def _visible_record(value):
    """Statistical evidence is visible; service configuration/audit stays server-side."""
    hidden = {'provider', 'base_url', 'api_url', 'endpoint', 'model', 'model_name', 'model_id',
              'returned_model', 'response_model', 'configured_model', 'response_id', 'request_id',
              'usage', 'api_status', 'generation', 'dialogue', 'connection', 'headers', 'calls', 'raw_response'}
    if isinstance(value, dict):
        return {key: _visible_record(item) for key, item in value.items()
                if str(key).lower() not in hidden
                and not re.search(r'key|token|secret|password|authorization|credential', str(key), re.I)}
    if isinstance(value, list):
        return [_visible_record(item) for item in value]
    return value


def _dialogue_status(research_agent, public_status=None):
    reader = getattr(research_agent, 'dialogue_status', None)
    if callable(reader):
        value = reader()
        if isinstance(value, dict):
            return value
    return (public_status or {}).get('dialogue') or {}


def _service_label(dialogue):
    state = (dialogue.get('connection') or {}).get('status')
    if state == 'ok':
        return 'Service is ready'
    if dialogue.get('configured') and state not in ('failed', 'not_configured'):
        return 'Service status not yet confirmed. It will update after an actual reply.'
    return 'The service is not ready. Please check the server configuration.'


def _display_message(value):
    message = re.sub(r'请先在模型设置中保存并测试连接[。.]', 'The service is not ready. Please check the server configuration.', str(value))
    message = re.sub(r'尚未连接真实 API，请在模型设置中填写模型名称和密钥[。.]', 'The service is not ready. Please check the server configuration.', message)
    return re.sub(r'请在模型连接设置的密码输入框填写密钥，不要把密钥作为研究问题发送[。.]', 'Check the server configuration. Do not send credentials in chat.', message)


def _generation_label(record):
    return 'Model response' if isinstance(record, dict) and record.get('status') == 'ok' else 'Local explanation'


def _plot(rows, comparisons):
    """Dot = survey estimate, capsule = its 95% CI, diamond = model mean."""
    rows = [r for r in rows if r.get('status') == 'ok' and finite(r.get('estimate'))]
    if not rows:
        return
    models = {r['group_id']: r.get('model_mean') for r in comparisons if finite(r.get('model_mean'))}
    maximum = max([r['ci_high'] for r in rows] + list(models.values()))
    maximum = min(1, max(.1, math.ceil(maximum * 10) / 10))

    def x(value):
        return f'{min(100, max(0, value / maximum * 100)):.2f}%'

    grid = ''.join(f'<i class="grid" style="left:{tick * 25}%"></i>' for tick in range(5))
    key = ['<span><i class="nh-key-range"></i><i class="nh-key-dot"></i>Survey estimates and 95% confidence intervals</span>']
    if models:
        key.append('<span><i class="nh-key-model"></i>Model mean</span>')
    parts = ['<div class="nh-card nh-chart"><div class="nh-chart-head"><div><h3>Fair or poor self-rated health</h3><p>Each row is a group; the band represents uncertainty in its survey estimate.</p></div>'
             '<div class="nh-chart-key">' + ''.join(key) + '</div></div><div class="nh-plot" role="img" aria-label="Survey estimates and independent model judgments">']
    for row in rows:
        label = _row_label(row)
        tip = f'{label}: {pct(row["estimate"])} (95% CI {pct(row["ci_low"])} – {pct(row["ci_high"])})'
        marks = (f'<i class="range" style="left:{x((row["ci_low"] + row["ci_high"]) / 2)};width:{x(row["ci_high"] - row["ci_low"])}"></i>'
                 f'<i class="dot" style="left:{x(row["estimate"])}"></i>')
        if row['group_id'] in models:
            tip += f' · Model mean {pct(models[row["group_id"]])}'
            marks += f'<i class="model" style="left:{x(models[row["group_id"]])}"></i>'
        parts.append(f'<div class="nh-plot-row" title="{esc(tip)}"><div class="nh-plot-label">{esc(label)}</div>'
                     f'<div class="nh-plot-track">{grid}{marks}</div><div class="nh-plot-value">{pct(row["estimate"])}</div></div>')
    ticks = ''.join(f'<span style="left:{tick * 25}%">{maximum * tick / 4 * 100:g}%</span>' for tick in range(5))
    parts.append(f'<div class="nh-plot-row axis" aria-hidden="true"><div class="nh-plot-label"></div><div class="nh-plot-track">{ticks}</div><div class="nh-plot-value"></div></div></div></div>')
    _html(''.join(parts))


def _coefficient_table(regression, unadjusted=False):
    model = regression.get('unadjusted', {}) if unadjusted else regression
    _section('Unadjusted education model on the same sample' if unadjusted else 'Adjusted odds ratios (OR)', f'Complete-case n={model.get("n", regression.get("n")):,}')
    _table([{'Term': row['term'], 'OR': f'{row["odds_ratio"]:.3f}',
             '95% CI': f'{row["ci_low"]:.3f} – {row["ci_high"]:.3f}',
             'Log odds': f'{row["log_odds"]:.4f}', 'SE': f'{row["se"]:.4f}'}
            for row in model.get('coefficients', [])])


def show_result(result, question=None, *, show_answer=True, key_prefix="", show_heading=True):
    result = _visible_record(result)
    success = result.get('status') == 'ok' and result.get('survey', {}).get('status') == 'ok'
    run_id = result.get('run_id')
    if show_heading:
        _html('<div class="nh-result-head"><div class="nh-eyebrow">' + ('Analysis results' if success else 'No result generated') + '</div>'
              + ('<h2>' + esc(question) + '</h2>' if question else '')
              + ('<div class="nh-run-id">' + esc(run_id) + '</div>' if run_id else '') + '</div>')
    if not success:
        message = result.get('answer', 'The analysis did not finish. See the run record.')
        if show_answer:
            if result.get('status') == 'clarify':
                st.info(message)
            else:
                st.warning(message)
        if run_id:
            st.download_button('Download run record', json.dumps(result, ensure_ascii=False, indent=2),
                               file_name=run_id + '.json', mime='application/json', key=key_prefix + 'blocked_' + run_id)
        return
    survey = result['survey']
    rows = survey.get('rows', [])
    valid = [r for r in rows if r.get('status') == 'ok']
    regression = survey.get('regression', {})
    tabs = st.tabs(['Overview', 'Statistics', 'Model comparison', 'Methods and records'])
    with tabs[0]:
        if valid:
            _plot(valid, result.get('comparison', []))
        if regression.get('status') == 'ok':
            _coefficient_table(regression)
        notes = [line for line in str(result.get('answer', '')).splitlines() if line.strip()]
        if notes and show_answer:
            _html('<div class="nh-card nh-note"><h3>Interpretation</h3>' + ''.join('<p>' + esc(line) + '</p>' for line in notes) + '</div>')
        contrasts = survey.get('gradient', {}).get('contrasts', [])
        if contrasts:
            _section('Group gradients', 'The difference between two group proportions, in percentage points.')
            _table([{'Group contrast': _row_label(r), 'Difference (pp)': pp(r.get('estimate')) if r.get('status') == 'ok' else '—',
                     '95% CI': pp(r.get('ci_low')) + ' – ' + pp(r.get('ci_high')) if r.get('status') == 'ok' else 'Suppressed'}
                    for r in contrasts])
    with tabs[1]:
        if rows:
            _section('Group estimates', 'Weighted proportions, standard errors, and 95% confidence intervals. Cells that fail release checks do not display estimates.')
            _table([{'Group': _row_label(r), 'Unweighted n': f'{r["n"]:,}' if finite(r.get('n')) else r.get('n'),
                     'Weighted proportion': pct(r.get('estimate')) if r.get('status') == 'ok' else '—',
                     '95% CI': pct(r.get('ci_low')) + ' – ' + pct(r.get('ci_high')) if r.get('status') == 'ok' else '—',
                     'SE (pp)': f'{r["se"] * 100:.3f}' if r.get('status') == 'ok' else None,
                     'Release status': 'Passed' if r.get('status') == 'ok' else r.get('suppression_reason', 'Suppressed')}
                    for r in rows])
        if regression.get('status') == 'ok':
            _coefficient_table(regression)
            _coefficient_table(regression, True)
            st.caption('ORs are not risk ratios. Adjusted and unadjusted models use the same complete-case sample.')
    with tabs[2]:
        measurement = result.get('measurement', {})
        comparisons = result.get('comparison', [])
        if comparisons:
            _section('Model estimates and survey evidence', 'Model responses are measured independently and are not population health statistics.')
            _table([{'Group': _row_label(r), 'Survey estimate': pct(r['survey']), 'Model mean': pct(r['model_mean']),
                     'Model − survey (pp)': f'{r["gap_pp"]:+.2f}', 'Within survey 95% CI': 'Yes' if r['inside_ci'] else 'No',
                     'Valid / planned calls': str(r.get('valid_count')) + ' / ' + str(r.get('total_count'))}
                    for r in comparisons])
        else:
            note = measurement.get('message') or 'This analysis computed the real survey benchmark only. Enable the independent benchmark for additional model calls.'
            if re.search(r'模型设置|接口密钥|模型名称|API Key|保存.*连接', note):
                note = 'The service is not ready. Please check the server configuration.'
            st.info(note)
        if result.get('gradient_comparison'):
            _section('Gradient comparison', 'The model–survey gradient gap for the same group contrasts.')
            _table([{'Group contrast': _row_label(r), 'Survey gradient (pp)': f'{r["survey_gradient_pp"]:+.2f}',
                     'Model gradient (pp)': f'{r["model_gradient_pp"]:+.2f}', 'Gradient gap (pp)': f'{r["gradient_gap_pp"]:+.2f}'}
                    for r in result['gradient_comparison']])
        with st.expander('Independent benchmark summary'):
            st.json(measurement)
    with tabs[3]:
        _html('<div class="nh-card nh-note"><h3>Registered research scope</h3><p>NHIS 2024 · U.S. adults aged 25–64 · PHSTAT_A = 4 or 5 · Construct the full design before domain subsetting.</p>'
              '<p class="nh-muted">R/Stata numerical agreement and external NCHS comparisons await independent verification.</p></div>')
        with st.expander('Analysis specification and data provenance'):
            st.json({'spec': result.get('spec'), 'data': result.get('data'), 'provenance': survey.get('provenance')})
        with st.expander('Denominators, missing data, and release checks'):
            st.json({k: survey.get(k) for k in ['denominators', 'missingness', 'missingness_by_code', 'design', 'imputation', 'checks', 'warnings']})
        with st.expander('Statistical record'):
            st.json(result)
    with st.container(key=(key_prefix + 'exports'), horizontal=True, gap='small'):
        st.download_button('Export report', markdown_report(result), file_name=run_id + '.md', mime='text/markdown', key=key_prefix + 'report_' + run_id)
        st.download_button('Export data table', csv_report(result), file_name=run_id + '.csv', mime='text/csv', key=key_prefix + 'csv_' + run_id)
        st.download_button('Export statistical record', json.dumps(result, ensure_ascii=False, indent=2), file_name=run_id + '.json', mime='application/json', key=key_prefix + 'json_' + run_id)


def _new_conversation():
    conversation = st.session_state.nhis_conversations.create()
    st.session_state.active_conversation_id = conversation['id']
    st.session_state.workspace_page = 'Chat'
    st.session_state.conversation_search = ''
    st.session_state.chat_draft = ''
    st.session_state.chat_open_result = None


def _resume_conversation(conversation_id):
    st.session_state.active_conversation_id = conversation_id
    st.session_state.chat_draft = ''
    st.session_state.chat_open_result = None
    st.session_state.workspace_page = 'Chat'


def _request_chat_deletion(conversation_id=None, title=''):
    if not st.session_state.get('chat_turn_busy'):
        st.session_state.pending_chat_deletion = {'id': conversation_id, 'title': title}


def _cancel_chat_deletion():
    st.session_state.pop('pending_chat_deletion', None)


def _confirm_chat_deletion():
    pending = st.session_state.get('pending_chat_deletion')
    if not isinstance(pending, dict) or st.session_state.get('chat_turn_busy'):
        return
    conversation_id = pending.get('id')
    service = st.session_state.nhis_conversations
    missing = False
    try:
        result = service.clear() if conversation_id is None else service.delete(conversation_id)
    except FileNotFoundError:
        if conversation_id is None:
            pending['error'] = 'The saved chat list is unavailable. Refresh and try again.'
            return
        missing = True
        result = {'deleted': [conversation_id], 'deleted_count': 0}
    except (RuntimeError, ValueError):
        pending['error'] = 'A chat is still being processed. Wait for it to finish, then try again. No chats were deleted.'
        return
    except Exception:
        pending['error'] = 'The chats could not be deleted. Please try again.'
        return
    removed = result.get('deleted', []) if isinstance(result, dict) else []
    active = st.session_state.get('active_conversation_id')
    if conversation_id is None or conversation_id == active or active in removed:
        st.session_state.pop('active_conversation_id', None)
        st.session_state.chat_open_result = None
        st.session_state.pop('chat_composer', None)
        st.session_state.chat_draft = ''
        try:
            _new_conversation()
        except Exception:
            st.session_state.chat_deletion_notice = 'The chats were deleted. Select New chat to start again.'
    if conversation_id is None or st.session_state.get('history_conversation') in removed:
        st.session_state.pop('history_conversation', None)
    st.session_state.pop('pending_chat_deletion', None)
    st.session_state.setdefault('chat_deletion_notice', 'This chat no longer exists; it was removed from the list.' if missing
                                else 'All chats deleted. Analysis records remain.' if conversation_id is None
                                else 'Chat deleted. Analysis records remain.')


@st.dialog('Delete chats', dismissible=False)
def _chat_deletion_dialog():
    pending = st.session_state.get('pending_chat_deletion')
    if not isinstance(pending, dict):
        st.rerun()
        return
    clear_all = pending.get('id') is None
    st.write('Permanently delete every saved chat?' if clear_all else 'Permanently delete this chat?')
    if not clear_all:
        st.text(str(pending.get('title') or 'New chat'))
    st.caption('This cannot be undone. Analysis records remain available.')
    if pending.get('error'):
        st.warning(pending['error'])
    busy = bool(st.session_state.get('chat_turn_busy'))
    with st.container(horizontal=True, gap='small'):
        st.button('Cancel', key='cancel_chat_deletion', on_click=_cancel_chat_deletion, disabled=busy)
        st.button('Clear all chats' if clear_all else 'Delete chat', key='confirm_chat_deletion',
                  type='primary', on_click=_confirm_chat_deletion, disabled=busy)


def _select_chat_result(run_id):
    st.session_state.chat_open_result = run_id


def _event_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone().strftime('%H:%M:%S')
    except (ValueError, TypeError):
        return ''


def _event_facts(event):
    labels = {'full_sample_n': 'Full adult sample', 'age_domain_n': 'Age domain',
              'filtered_domain_n': 'After filters', 'valid_outcome_n': 'Valid outcome',
              'sample_n': 'Sample size', 'n': 'Sample size', 'row_count': 'Records', 'records': 'Records',
              'groups': 'Groups', 'group_count': 'Groups', 'released_groups': 'Released groups',
              'suppressed_groups': 'Suppressed groups', 'released_cells': 'Released groups', 'suppressed_cells': 'Suppressed groups',
              'imputations': 'Income imputations', 'imputation_count': 'Income imputations',
              'planned_calls': 'Planned calls', 'valid_calls': 'Valid calls', 'completed_calls': 'Completed calls',
              'strata': 'Strata', 'psus': 'PSUs', 'design_df': 'Design degrees of freedom',
              'source_run_id': 'Source run', 'runtime': 'Statistical engine',
              'r_version': 'R version', 'survey_version': 'survey version'}
    facts = _visible_record(event.get('facts') or {})
    if not isinstance(facts, dict):
        return ''
    parts = []
    for key, value in facts.items():
        if key == 'cache_hit' and isinstance(value, bool):
            parts.append('Cache reused ' + ('Yes' if value else 'No'))
        elif key == 'elapsed_ms' and finite(value):
            parts.append(f'Duration {value / 1000:.1f} s')
        elif finite(value):
            parts.append(labels.get(str(key), str(key).replace('_', ' ')) + ' ' + f'{value:,}')
        elif key in ('source_run_id', 'runtime', 'r_version', 'survey_version') and isinstance(value, str):
            parts.append(labels[key] + ' ' + value[:100])
    return ' · '.join(parts)


def _process_view(process):
    events = process if isinstance(process, list) else process.get('events', []) if isinstance(process, dict) else []
    stages = {}
    for event in events:
        if isinstance(event, dict):
            stages[str(event.get('stage') or event.get('label') or len(stages))] = event
    if not stages:
        return
    statuses = {'running': 'Processing', 'complete': 'Complete', 'blocked': 'Scope clarification needed',
                'failed': 'Incomplete', 'skipped': 'Skipped'}
    with st.expander('Calculation steps', expanded=False):
        for event in stages.values():
            stamp = _event_time(event.get('timestamp'))
            label = _process_label(event)
            state = statuses.get(event.get('status'), '')
            st.markdown('**' + label + '**' + (' · ' + state if state else ''))
            details = [stamp, str(event.get('detail') or ''), _event_facts(event)]
            if finite(event.get('elapsed_ms')):
                details.append(f'{event["elapsed_ms"] / 1000:.1f} s')
            st.caption(' · '.join(part for part in details if part))


def _conversation_context(conversation):
    context = conversation.get('context') or {}
    if not isinstance(context, dict):
        return ''
    spec = context.get('spec') or context.get('last_spec') or context.get('analysis_spec') or context
    if not isinstance(spec, dict) or not spec.get('group_by'):
        return ''
    grouping = {'overall': 'Overall', 'education': 'By education', 'race': 'By race and ethnicity',
                'sex': 'By sex', 'age': 'By age group', 'education_race': 'Education × race and ethnicity', 'income': 'By income'}
    labels = {'female': 'Female', 'male': 'Male', '25_34': '25–34 years', '35_44': '35–44 years',
              '45_54': '45–54 years', '55_64': '55–64 years', 'less_than_high_school': 'Less than high school',
              'high_school': 'High school or GED', 'some_college': 'Some college or associate degree', 'bachelors_plus': 'Bachelor’s degree or higher',
              'hispanic': 'Hispanic', 'nh_white': 'Non-Hispanic White', 'nh_black': 'Non-Hispanic Black',
              'nh_asian': 'Non-Hispanic Asian', 'nh_other': 'Non-Hispanic other or multiple race',
              'below_100': 'Below 100% of the poverty threshold', '100_199': '100% to <200% of the poverty threshold',
              '200_399': '200% to <400% of the poverty threshold', '400_plus': '400%+ of the poverty threshold'}
    parts = ['NHIS 2024', 'U.S. adults aged 25–64', grouping.get(spec['group_by'], spec['group_by'])]
    for value in (spec.get('filters') or {}).values():
        values = value if isinstance(value, list) else [value]
        parts.extend(labels.get(str(item), str(item)) for item in values)
    if spec.get('analysis') == 'regression':
        parts.append('Reduced regression excluding income' if spec.get('adjustment') == 'no_income' else 'Fully adjusted regression')
    elif spec.get('analysis') == 'gradient':
        parts.append('Group gradients')
    return ' · '.join(parts)


def _chat_result(conversation, run_id):
    latest = conversation.get('latest_result')
    if isinstance(latest, dict) and latest.get('run_id') == run_id:
        return latest
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', run_id):
        return None
    try:
        result = json.loads((ROOT / 'runs' / run_id / 'result.json').read_text(encoding='utf-8'))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def _render_conversation(conversation):
    messages = conversation.get('messages') or []
    assistant_messages = [message for message in messages if message.get('role') == 'assistant']
    latest_assistant = assistant_messages[-1].get('id') if assistant_messages else None
    selected_run = st.session_state.get('chat_open_result') or conversation.get('last_run_id')
    rendered_runs = set()
    for index, message in enumerate(messages):
        role = message.get('role')
        if role not in ('user', 'assistant'):
            continue
        prefix = 'chat_' + conversation['id'] + '_' + str(message.get('id') or index) + '_'
        with st.container(key=prefix + role + '_message'), st.chat_message(role):
            st.markdown(_display_message(message.get('content') or '') if role == 'assistant' else str(message.get('content') or ''))
            if role != 'assistant':
                continue
            generation = message.get('generation')
            st.caption(_generation_label(generation))
            _process_view(message.get('process'))
            run_id = message.get('run_id')
            if run_id and run_id not in rendered_runs:
                rendered_runs.add(run_id)
                with st.expander('View statistical results', expanded=False):
                    if selected_run == run_id:
                        result = _chat_result(conversation, run_id)
                        if result is not None:
                            show_result(result, show_answer=False, key_prefix=prefix, show_heading=False)
                        else:
                            st.caption('Detailed records for this run are currently unavailable.')
                    else:
                        st.button('Load this statistical result', key=prefix + 'load_result',
                                  on_click=_select_chat_result, args=(run_id,), type='tertiary')
            # Streamlit's native code block provides a working clipboard button.
            # Keep that text view inside a popover rather than duplicating it in the transcript.
            with st.container(key=prefix + 'actions', horizontal=True, gap='small'):
                with st.popover('Copy reply', icon=':material/content_copy:'):
                    st.caption('Use the copy icon in the top-right corner to copy this reply.')
                    st.code(_display_message(message.get('content') or ''), language=None, wrap_lines=True)
            if message.get('id') == latest_assistant:
                suggestions = message.get('suggestions') or []
                if suggestions:
                    with st.container(key='chat_suggestions', horizontal=True, gap='small'):
                        for suggestion_index, suggestion in enumerate(suggestions[:3]):
                            if isinstance(suggestion, dict):
                                text = str(suggestion.get('text') or suggestion.get('value') or suggestion.get('label') or '')
                                label = str(suggestion.get('label') or text)
                            else:
                                text = label = str(suggestion)
                            if text:
                                st.button(label, key=prefix + 'suggestion_' + str(suggestion_index),
                                          on_click=_choose_example, args=(text,))


if (st.session_state.get('nhis_config_source') != 'server-environment-v1' or
        'nhis_agent' not in st.session_state or
        not callable(getattr(st.session_state.nhis_agent, 'history', None)) or
        not callable(getattr(st.session_state.nhis_agent, 'analyze_spec', None)) or
        not callable(getattr(st.session_state.nhis_agent, 'decide_reply', None)) or
        not callable(getattr(st.session_state.nhis_agent, 'dialogue_status', None))):
    st.session_state.nhis_agent = Agent(root=ROOT)
    st.session_state.nhis_config_source = 'server-environment-v1'
for obsolete_field in ('connection_api_key', 'connection_provider', 'connection_base_url',
                       'connection_model_name', 'connection_notice'):
    if obsolete_field in st.session_state:
        del st.session_state[obsolete_field]
st.session_state.setdefault('repeats', 5)
st.session_state.setdefault('versions', 3)
agent = st.session_state.nhis_agent
if ('nhis_conversations' not in st.session_state or
        st.session_state.get('nhis_conversation_agent') is not agent or
        not callable(getattr(st.session_state.nhis_conversations, 'send', None))):
    st.session_state.nhis_conversations = ConversationService(agent)
    st.session_state.nhis_conversation_agent = agent
conversations = st.session_state.nhis_conversations
if st.session_state.get('nhis_ui_language_version') != 'en-v2':
    st.session_state.active_conversation_id = conversations.create()['id']
    st.session_state.nhis_ui_language_version = 'en-v2'
elif not st.session_state.get('active_conversation_id'):
    st.session_state.active_conversation_id = conversations.create()['id']
try:
    status = agent.status()
except Exception:
    st.error('Unable to read the local engine status. Refresh and try again.')
    st.stop()
data = status.get('data', {})
dialogue = _dialogue_status(agent, status)
statistics = status.get('statistics', {})
ready = data.get('ready') and statistics.get('ready')
repeats, versions = int(st.session_state.repeats), int(st.session_state.versions)

with st.sidebar:
    _html('<div class="nh-sidebar-brand">NHIS <span>Lab</span></div>')
    chat_busy = bool(st.session_state.get('chat_turn_busy'))
    st.button('New chat', key='new_conversation', icon=':material/edit_square:',
              on_click=_new_conversation, width='stretch', disabled=chat_busy)
    search = st.text_input('Search chats', placeholder='Search chats', key='conversation_search', label_visibility='collapsed')
    _html('<div class="nh-sidebar-label">Chats</div>')
    saved_conversations = conversations.list()
    matched = []
    query = search.strip().casefold()
    for item in saved_conversations:
        if not item.get('message_count'):
            continue
        if not query or query in str(item.get('title') or '').casefold():
            matched.append(item)
        elif query:
            try:
                saved = conversations.get(item['id'])
                if any(query in str(message.get('content') or '').casefold() for message in saved.get('messages', [])):
                    matched.append(item)
            except (OSError, ValueError, KeyError):
                continue
    with st.container(key='sidebar_history'):
        if query and not matched:
            st.caption('No matching chats.')
        elif not matched:
            st.caption('Your chats are saved here.')
        for item in matched:
            title = str(item.get('title') or 'New chat')
            with st.container(horizontal=True, gap='small'):
                st.button(title, key='sidebar_conversation_' + item['id'], help=title,
                          type='secondary' if item['id'] == st.session_state.active_conversation_id else 'tertiary',
                          width='stretch', on_click=_resume_conversation,
                          args=(item['id'],), disabled=chat_busy)
                st.button('Delete', key='delete_conversation_' + item['id'], help='Delete chat: ' + title,
                          type='tertiary', on_click=_request_chat_deletion, args=(item['id'], title), disabled=chat_busy)
    st.button('Clear all chats', key='clear_all_chats', type='tertiary',
              on_click=_request_chat_deletion, disabled=chat_busy or not saved_conversations)
    if st.session_state.get('chat_deletion_notice'):
        st.caption(st.session_state.pop('chat_deletion_notice'))
    _html('<div class="nh-sidebar-divider"></div>')
    if st.session_state.get('workspace_page') not in PAGES:
        st.session_state.workspace_page = PAGES[0]
    page = st.radio('Page', PAGES, label_visibility='collapsed', key='workspace_page')
    with st.expander('Research options', expanded=False):
        measure_model = st.toggle('Independent benchmark', value=False, key='measure_model')
        st.number_input('Repetitions per prompt', min_value=1, max_value=5, value=repeats, step=1,
                        key='measure_repeats', on_change=_keep_settings)
        st.number_input('Prompt wording versions', min_value=1, max_value=3, value=versions, step=1,
                        key='measure_versions', on_change=_keep_settings)
        st.caption(f'Planned extra benchmark calls per group: {repeats * versions} ; no survey estimates or chat history are shared. This switch does not control chat replies.')
        dialogue_status_slot = st.empty()
        dialogue_status_slot.caption(_service_label(dialogue))
        st.caption('Adult data and statistical engine ready' if ready else 'Data or statistical environment needs preparation')
        if st.button('Refresh status', key='refresh_connections', width='stretch'):
            st.rerun()
    st.caption('NHIS 2024 · U.S. adults aged 25–64 · Descriptive research')

if st.session_state.get('pending_chat_deletion'):
    _chat_deletion_dialog()

if page == 'Chat':
    try:
        conversation = conversations.get(st.session_state.active_conversation_id)
    except (OSError, ValueError, KeyError):
        conversation = None
    if not isinstance(conversation, dict):
        conversation = conversations.create()
        st.session_state.active_conversation_id = conversation['id']
    chat_header_slot = st.empty()
    chat_header_slot.markdown('<div class="nh-chat-header">NHIS Lab <span>' + esc('Assistant') + '</span></div>', unsafe_allow_html=True)
    # The native composer is instantiated before rendering the transcript. That
    # lets one submitted turn update the local conversation without an extra rerun.
    if 'chat_draft' in st.session_state:
        st.session_state.chat_composer = st.session_state.pop('chat_draft')
    question = st.chat_input('Message NHIS Lab', key='chat_composer', max_chars=2000)
    progress_slot = st.empty()
    if question and question.strip():
        st.session_state.chat_turn_busy = True
        with progress_slot.container():
            try:
                with st.status('Working…', expanded=False) as progress:
                    current_detail = st.empty()

                    def on_event(event):
                        progress.update(label=_process_label(event), state='running')
                        detail = ' · '.join(part for part in (str(event.get('detail') or ''), _event_facts(event)) if part)
                        if detail:
                            current_detail.caption(detail)

                    reply = conversations.send(conversation['id'], question,
                        measure_model=measure_model, repeats=repeats, prompt_versions=versions,
                        on_event=on_event)
                    conversation = reply.get('conversation') or conversations.get(conversation['id'])
                    dialogue = _dialogue_status(agent)
                    dialogue_status_slot.caption(_service_label(dialogue))
                    chat_header_slot.markdown('<div class="nh-chat-header">NHIS Lab <span>' + esc('Assistant') + '</span></div>', unsafe_allow_html=True)
                    message = reply.get('message') or {}
                    progress.update(label='Reply ready',
                        state='error' if message.get('status') in ('failed', 'error') else 'complete')
                progress_slot.empty()
            except Exception:
                st.error('This message could not be completed. Your chat is saved; you can try again later.')
            finally:
                st.session_state.chat_turn_busy = False
    if not conversation.get('messages'):
        _html('<div class="nh-chat-welcome"><h1>What can I help you with?</h1><p>Chat, write, explain, or explore a question with NHIS data.</p></div>')
        with st.container(key='starter_chips', horizontal=True, gap='small', horizontal_alignment='center'):
            examples = [
                ('Compare education groups', 'Compare fair or poor self-rated health by education among adults aged 25–64.'),
                ('Explore the income gradient', 'Compare fair or poor self-rated health proportions and income gradients among adults aged 25–64.'),
                ('Help me write', 'Help me write a clear, friendly email.'),
            ]
            for index, (label, starter_question) in enumerate(examples):
                st.button(label, key='starter_' + str(index), on_click=_choose_example, args=(starter_question,))
    else:
        preview = _conversation_context(conversation)
        if preview:
            with st.container(key='chat_scope'):
                st.caption(preview)
    with st.container(key='conversation'):
        _render_conversation(conversation)
    if not ready:
        with st.expander('Data or statistical environment needs preparation', expanded=False):
            st.caption(data.get('message') if not data.get('ready') else statistics.get('reason', 'The statistical environment needs installation.'))
            if st.button('Prepare official data', key='prepare_official_data'):
                agent.setup_data()
                st.info('Data preparation has started. You can explore the help information while it runs.')

elif page == 'Analysis history':
    _page_head('Analysis history', 'Open saved runs to review their estimates, model comparisons, and methods.')
    saved_conversations = conversations.list()
    if saved_conversations:
        with st.expander('Resume a saved chat', expanded=False):
            conversation_choices = {item['id']: item for item in saved_conversations}
            selected_conversation = st.selectbox('Choose a chat', list(conversation_choices),
                format_func=lambda cid: str(conversation_choices[cid].get('title') or 'New chat'),
                key='history_conversation')
            st.button('Resume chat', key='resume_history_conversation',
                      on_click=_resume_conversation, args=(selected_conversation,))
    history = agent.history()
    if not history:
        st.info('No analyses have been saved yet. Completed research runs will appear here.')
    else:
        choices = {r['run_id']: r for r in history}
        selected = st.selectbox('Choose an analysis record', list(choices), format_func=lambda rid: choices[rid]['question'][:70] + ' · ' + rid)
        try:
            saved = json.loads((ROOT / 'runs' / selected / 'result.json').read_text())
            show_result(saved, choices[selected]['question'])
        except (OSError, ValueError):
            st.warning('This record is incomplete and cannot be opened right now.')

elif page == 'Data and methods':
    _page_head('Data and methods', 'Research starts with traceable inputs. Keep variables and populations fixed, and record every filter and denominator.')
    stats = [('Real adult sample', f'{data.get("row_count"):,}' if data.get('row_count') else 'Not ready'),
             ('Income imputations', '10 / 10' if data.get('income_ready') else 'Pending verification'),
             ('R survey engine', statistics.get('survey_version') if statistics.get('ready') else 'Not ready')]
    _html('<div class="nh-stats">' + ''.join('<div class="nh-card"><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>' for label, value in stats) + '</div>')
    descriptions = [('01', 'Real survey data', 'Official Sample Adult file. Preserve the full survey design before selecting the age 25–64 domain.'),
                    ('02', 'Independent model benchmark', 'The model sees group definitions only. Use repeated English prompts and retain refusals and failures.'),
                    ('03', 'Compare estimates', 'Calculate percentage-point gaps and group gradients, retaining intervals and run records.')]
    _html('<div class="nh-block-head"><h2>A traceable research workflow</h2></div><div class="nh-methods">'
          + ''.join(f'<div class="nh-card"><b>{number}</b><strong>{title}</strong><p>{text}</p></div>' for number, title, text in descriptions) + '</div>')
    _html('<div class="nh-block-head"><h2>Research scope and release rules</h2></div><div class="nh-card nh-note">'
          '<p>The population is U.S. adults aged 25–64, with fair or poor self-rated health as the outcome. Report unweighted n, weighted proportions, standard errors, and 95% confidence intervals. Suppress cells with n below 100 or other failed reliability checks.</p>'
          '<p class="nh-muted">Cross-sectional associations are not causal effects. Real-data calculation is available; R/Stata agreement and external NCHS comparisons await independent verification.</p></div>')
    if st.button('Download or verify official data', width='content'):
        agent.setup_data()
        st.info('Real-data preparation has started. Refresh the status shortly.')
    with st.expander('Data sources, checksums, and download status'):
        st.json(data)
    with st.expander('Full variable registry and statistical rules'):
        st.json(status.get('registry', {}))
