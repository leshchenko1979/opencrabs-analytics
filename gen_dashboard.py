#!/usr/bin/env python3
"""RSI/Brain Analytics Dashboard Generator.

Dual-profile support with UI switcher. Template lives in template.html.
"""
import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

HOME = Path.home()
OPENCRABS_ROOT = HOME / '.opencrabs'
PROFILES_DIR = OPENCRABS_ROOT / 'profiles'
RSI_DIR = HOME / '.opencrabs' / 'rsi'
DB_PATH = HOME / '.opencrabs' / 'opencrabs.db'
OUTPUT_DEFAULT = HOME / '.opencrabs' / 'analytics' / 'index.html'
TEMPLATE_PATH = Path(__file__).parent / 'template.html'


def discover_profiles():
    """Dynamically discover profiles from the filesystem."""
    profiles = {}
    if OPENCRABS_ROOT.exists():
        profiles['default'] = OPENCRABS_ROOT
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if d.is_dir() and '.bak' not in d.name and not d.name.startswith('.'):
                profiles[d.name] = d
    return profiles


def parse_md_sections(filepath):
    """Parse markdown file into sections with heading and size."""
    sections = []
    try:
        content = filepath.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return sections
    lines = content.split('\n')
    current_heading = '(preamble)'
    current_lines = []
    for line in lines:
        m = re.match(r'^(#{1,6})\s+(.+)', line)
        if m:
            if current_lines or current_heading != '(preamble)':
                text = '\n'.join(current_lines).strip()
                kb = round(len(text.encode('utf-8')) / 1024, 2)
                if kb > 0 or current_heading == '(preamble)':
                    sections.append({'heading': current_heading, 'kb': kb})
            current_heading = m.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    # last section
    text = '\n'.join(current_lines).strip()
    kb = round(len(text.encode('utf-8')) / 1024, 2)
    if kb > 0 or not sections:
        sections.append({'heading': current_heading, 'kb': kb})
    # sort largest first
    sections.sort(key=lambda s: s['kb'], reverse=True)
    return sections


def collect_brain_sizes(profile_path):
    sizes = {}
    if profile_path.exists():
        for f in sorted(profile_path.glob('*.md')):
            try:
                sizes[f.name] = round(f.stat().st_size / 1024, 1)
            except OSError:
                pass
    return dict(sorted(sizes.items(), key=lambda x: x[1], reverse=True))


def collect_brain_history(profile_path):
    history = defaultdict(list)
    if not profile_path.exists():
        return history
    bak_entries = []
    for bak in profile_path.glob('*.md.*.bak'):
        m = re.match(r'(.+\.md)\.(\d{4}-\d{2}-\d{2}T\d{6})\.bak', bak.name)
        if m:
            fname, ts_str = m.group(1), m.group(2)
            try:
                kb = round(bak.stat().st_size / 1024, 1)
                date_str = ts_str[5:7] + '-' + ts_str[8:10] + ' ' + ts_str[11:13] + ':00'
                bak_entries.append((ts_str, fname, date_str, kb))
            except OSError:
                pass
    bak_entries.sort(key=lambda x: x[0])
    for ts_str, fname, date_str, kb in bak_entries:
        iso_ts = ts_str[:4] + '-' + ts_str[5:7] + '-' + ts_str[8:10] + 'T' + ts_str[11:13] + ':' + ts_str[13:15] + ':' + ts_str[15:17]
        history[fname].append({'date': date_str, 'kb': kb, 'iso': iso_ts})
    return dict(history)


def collect_tool_stats():
    stats = {}
    if not DB_PATH.exists():
        return stats
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("""
            SELECT tool_name, COUNT(*) as total,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as fails,
                   MIN(created_at) as first_seen, MAX(created_at) as last_seen
            FROM tool_executions
            GROUP BY tool_name
        """)
        for name, total, fails, first_ts, last_ts in cur.fetchall():
            rate = round((fails / total) * 100, 1) if total > 0 else 0.0
            stats[name] = {'total': total, 'fails': fails, 'rate': rate,
                           'first_seen': first_ts, 'last_seen': last_ts}
        conn.close()
    except Exception as e:
        print(f'  ⚠️  DB query failed: {e}')
    return stats


def get_data_period(tool_stats):
    from datetime import datetime as dt
    all_first = [v['first_seen'] for v in tool_stats.values() if v.get('first_seen')]
    all_last = [v['last_seen'] for v in tool_stats.values() if v.get('last_seen')]
    if not all_first or not all_last:
        return 'Unknown'
    first_ts = min(all_first)
    last_ts = max(all_last)
    try:
        first_str = dt.utcfromtimestamp(int(first_ts)).strftime('%Y-%m-%d')
        last_str = dt.utcfromtimestamp(int(last_ts)).strftime('%Y-%m-%d')
    except (ValueError, OSError, OverflowError):
        return 'Unknown'
    if first_str == last_str:
        return first_str
    return f'{first_str} → {last_str}'


def collect_rsi_applied_per_tool():
    """Count actual improvement_applied events per tool from feedback_ledger."""
    counts = defaultdict(int)
    if not DB_PATH.exists():
        return counts
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT tool_name FROM tool_executions')
        known_tools = {r[0] for r in cur.fetchall()}
        cur.execute("""
            SELECT dimension, COUNT(*) as cnt
            FROM feedback_ledger
            WHERE event_type = 'improvement_applied'
            GROUP BY dimension
        """)
        rows = cur.fetchall()
        conn.close()
        for desc, cnt in rows:
            desc_lower = desc.lower()
            matched = False
            for tool in known_tools:
                if re.search(r'\b' + re.escape(tool.lower()) + r'\b', desc_lower):
                    counts[tool] += cnt
                    matched = True
            if not matched:
                counts['_unmatched'] += cnt
    except Exception as e:
        print(f'  ⚠️  Applied query failed: {e}')
    return dict(counts)


def collect_rsi_improvements_per_tool():
    """Count actual improvement entries per tool from RSI logs."""
    counts = defaultdict(int)
    imp_file = RSI_DIR / 'improvements.md'
    files_to_parse = []
    if imp_file.exists():
        files_to_parse.append(imp_file)
    history_dir = RSI_DIR / 'history'
    if history_dir.exists():
        files_to_parse.extend(sorted(history_dir.glob('*.md')))

    tools_in_db = set()
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute('SELECT DISTINCT tool_name FROM tool_executions')
            tools_in_db = {r[0] for r in cur.fetchall()}
            conn.close()
        except Exception:
            pass

    for f in files_to_parse:
        content = f.read_text()
        entries = re.split(r'(?=\*\*Date:\*\*)', content)
        for entry in entries:
            if not entry.strip():
                continue
            entry_lower = entry.lower()
            for tool in tools_in_db:
                if re.search(r'\b' + re.escape(tool.lower()) + r'\b', entry_lower):
                    counts[tool] += 1
    return dict(counts)


def collect_rsi_highlights():
    highlights = []
    imp_file = RSI_DIR / 'improvements.md'
    if imp_file.exists():
        content = imp_file.read_text()
        for m in re.finditer(r'\*\*Date:\*\*\s+(\d{4}-\d{2}-\d{2})', content):
            highlights.append({'date': m.group(1), 'description': ''})
    history_dir = RSI_DIR / 'history'
    if history_dir.exists():
        for f in sorted(history_dir.glob('*.md')):
            m = re.match(r'(\d{4}-\d{2}-\d{2})', f.name)
            if m:
                hist_content = f.read_text()
                entry_count = len(re.findall(r'\*\*Date:\*\*\s+\d{4}-\d{2}-\d{2}', hist_content))
                if entry_count == 0:
                    entry_count = 1
                for _ in range(entry_count):
                    highlights.append({'date': m.group(1), 'description': ''})
    highlights.sort(key=lambda x: x['date'])
    return highlights


def build_profile_data(profile_name, profile_path):
    brain_sizes = collect_brain_sizes(profile_path)
    brain_history = collect_brain_history(profile_path)
    now = datetime.now(timezone.utc)
    today_display = now.strftime('%m-%d %H:00')
    today_iso = now.strftime('%Y-%m-%dT%H:%M:%S')
    for fname, kb in brain_sizes.items():
        if fname not in brain_history:
            brain_history[fname] = []
        brain_history[fname].append({'date': today_display, 'kb': kb, 'iso': today_iso})
    # Core files first, then 7 biggest others
    core_files = ['AGENTS.md', 'USER.md', 'SOUL.md']
    core_items = [(f, brain_sizes[f]) for f in core_files if f in brain_sizes]
    other_items = [(k, v) for k, v in brain_sizes.items() if k not in core_files]
    top_brain = core_items + other_items[:max(0, 10 - len(core_items))]
    other_kb = sum(v for k, v in list(brain_sizes.items())[10:])
    if other_kb > 0:
        top_brain.append(('Other', round(other_kb, 1)))
    brain_labels = [x[0] for x in top_brain]
    brain_kb = [x[1] for x in top_brain]
    top10_files = set(x[0] for x in top_brain) - {'Other'}
    brain_history_data = []
    for fname, points in sorted(brain_history.items(), key=lambda x: x[0]):
        if fname in top10_files:
            brain_history_data.append({'file': fname, 'points': points})
    # Section breakdown for top-10 files
    brain_sections = {}
    for fname in top10_files:
        fpath = profile_path / fname
        if fpath.exists():
            brain_sections[fname] = parse_md_sections(fpath)
    total_kb = round(sum(brain_sizes.values()), 1)
    # Core files size (AGENTS.md + USER.md + SOUL.md)
    core_files = ['AGENTS.md', 'USER.md', 'SOUL.md']
    core_kb = 0.0
    core_detail = {}
    for cf in core_files:
        cpath = profile_path / cf
        if cpath.exists():
            try:
                sz = round(cpath.stat().st_size / 1024, 1)
                core_detail[cf] = sz
                core_kb += sz
            except OSError:
                pass
    core_kb = round(core_kb, 1)

    return {
        'name': profile_name,
        'brain_labels': brain_labels,
        'brain_kb': brain_kb,
        'brain_history': brain_history_data,
        'brain_sections': brain_sections,
        'total_kb': total_kb,
        'core_kb': core_kb,
        'core_detail': core_detail,
        'file_count': len(brain_sizes),
    }


def build_tool_data(tool_stats, rsi_counts, rsi_applied, rsi_highlights):
    tools_sorted = sorted(tool_stats.items(), key=lambda x: x[1]['total'], reverse=True)
    top_used = tools_sorted[:10]
    least_used = sorted(tool_stats.items(), key=lambda x: x[1]['total'])[:10]
    highest_fail = sorted(tool_stats.items(), key=lambda x: x[1]['rate'], reverse=True)[:10]
    lowest_fail = sorted([(k, v) for k, v in tool_stats.items() if v['total'] >= 5], key=lambda x: x[1]['rate'])[:10]

    scatter_data = []
    for tool, st in tool_stats.items():
        rc = rsi_applied.get(tool, 0)
        r = max(3, min(30, rc * 2 + 3)) if rc > 0 else 3
        scatter_data.append({'x': st['total'], 'y': st['rate'], 'r': r, 'rsi': rc, 'label': tool})

    rsi_by_date = {}
    for h in rsi_highlights:
        rsi_by_date[h['date']] = rsi_by_date.get(h['date'], 0) + 1
    rsi_dates = sorted(rsi_by_date.keys())
    rsi_cum = []
    t = 0
    for d in rsi_dates:
        t += rsi_by_date[d]
        rsi_cum.append(t)

    most_used_name = top_used[0][0] if top_used else 'N/A'
    worst_fail_name = highest_fail[0][0] if highest_fail else 'N/A'
    worst_fail_rate = highest_fail[0][1]['rate'] if highest_fail else 0

    top_success = [t[1]['total'] - t[1]['fails'] for t in top_used]
    top_fails = [t[1]['fails'] for t in top_used]
    top_fail_rates = [t[1]['rate'] for t in top_used]
    top_applied = [rsi_applied.get(t[0], 0) for t in top_used]
    top_mentions = [rsi_counts.get(t[0], 0) for t in top_used]

    return {
        'scatter': scatter_data,
        'top_labels': [t[0] for t in top_used],
        'top_success': top_success,
        'top_fails': top_fails,
        'top_fail_rates': top_fail_rates,
        'top_applied': top_applied,
        'top_mentions': top_mentions,
        'least_list': [{'name': t[0], 'count': t[1]['total']} for t in least_used],
        'best_list': [{'name': t[0], 'rate': t[1]['rate'], 'count': t[1]['total']} for t in lowest_fail],
        'rsi_dates': rsi_dates,
        'rsi_cum': rsi_cum,
        'most_used': most_used_name,
        'worst_name': worst_fail_name,
        'worst_rate': worst_fail_rate,
        'total_rsi': len(rsi_highlights),
    }


def generate_html(profiles_data, tool_data, data_period):
    generated_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    def _safe_json(obj):
        """json.dumps escaped for safe embedding inside an HTML <script> block,
        so a '</script>' (or other markup) in a heading or tool name cannot break
        out of the script context."""
        return (json.dumps(obj)
                .replace('<', '\\u003c')
                .replace('>', '\\u003e')
                .replace('&', '\\u0026'))

    profiles_json = _safe_json(profiles_data)
    tool_json = _safe_json(tool_data)

    template = TEMPLATE_PATH.read_text()
    html = template.replace('__GENERATED_AT__', generated_at)
    html = html.replace('__DATA_PERIOD__', data_period)
    html = html.replace('__PROFILES_JSON__', profiles_json)
    html = html.replace('__TOOL_JSON__', tool_json)
    default_p = profiles_data[0]
    html = html.replace('__TOTAL_KB__', str(default_p['total_kb']))
    html = html.replace('__FILE_COUNT__', str(default_p['file_count']))
    html = html.replace('__TOTAL_RSI__', str(tool_data['total_rsi']))
    html = html.replace('__MOST_USED__', tool_data['most_used'])
    html = html.replace('__WORST_RATE__', str(tool_data['worst_rate']))
    html = html.replace('__WORST_NAME__', tool_data['worst_name'])
    return html


def main():
    parser = argparse.ArgumentParser(description='Generate RSI/Brain analytics dashboard')
    parser.add_argument('--output', '-o', default=str(OUTPUT_DEFAULT))
    args = parser.parse_args()

    print('Collecting data...')
    profiles_data = []
    for name, path in discover_profiles().items():
        pd = build_profile_data(name, path)
        profiles_data.append(pd)
        print(f'  Profile {name}: {pd["file_count"]} files, {pd["total_kb"]} KB')

    tool_stats = collect_tool_stats()
    print(f'  Tools tracked: {len(tool_stats)}')

    data_period = get_data_period(tool_stats)
    print(f'  Data period: {data_period}')

    rsi_counts = collect_rsi_improvements_per_tool()
    print(f'  Tools with RSI mentions: {len(rsi_counts)}')

    rsi_applied = collect_rsi_applied_per_tool()
    print(f'  Tools with RSI applied: {len(rsi_applied)}')

    rsi = collect_rsi_highlights()
    print(f'  RSI entries: {len(rsi)}')

    tool_data = build_tool_data(tool_stats, rsi_counts, rsi_applied, rsi)
    html = generate_html(profiles_data, tool_data, data_period)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f'✅ Dashboard written to {out}')


if __name__ == '__main__':
    main()
