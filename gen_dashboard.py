#!/usr/bin/env python3
"""RSI/Brain Analytics Dashboard Generator v6.

Dual-profile support with UI switcher. No f-strings for HTML/JS.
"""
import argparse
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

HOME = Path.home()
OPENCRABS_ROOT = HOME / '.opencrabs'
PROFILES_DIR = OPENCRABS_ROOT / 'profiles'


def discover_profiles():
    """Dynamically discover profiles from the filesystem."""
    profiles = {}
    # Root-level profile (redevest/default) — always included
    if OPENCRABS_ROOT.exists():
        profiles['default'] = OPENCRABS_ROOT
    # Scan profiles/ subdirectories (skip .bak dirs)
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if d.is_dir() and '.bak' not in d.name and not d.name.startswith('.'):
                profiles[d.name] = d
    return profiles
RSI_DIR = HOME / '.opencrabs' / 'rsi'
DB_PATH = HOME / '.opencrabs' / 'opencrabs.db'
OUTPUT_DEFAULT = HOME / '.opencrabs' / 'analytics' / 'index.html'


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
    # Collect all bak files with their timestamps
    bak_entries = []
    for bak in profile_path.glob('*.md.*.bak'):
        # Format: FILE.md.YYYY-MM-DDTHHMMSS.bak
        m = re.match(r'(.+\.md)\.(\d{4}-\d{2}-\d{2}T\d{6})\.bak', bak.name)
        if m:
            fname, ts_str = m.group(1), m.group(2)
            try:
                kb = round(bak.stat().st_size / 1024, 1)
                # Extract date+time for display (MM-dd HH:00), keep full timestamp for sorting
                date_str = ts_str[5:7] + '-' + ts_str[8:10] + ' ' + ts_str[11:13] + ':00'
                bak_entries.append((ts_str, fname, date_str, kb))
            except OSError:
                pass
    # Sort ALL entries by full timestamp first
    bak_entries.sort(key=lambda x: x[0])
    # Now build history dict in chronological order
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
    # created_at is unix epoch integer
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
        # Get known tool names
        cur.execute('SELECT DISTINCT tool_name FROM tool_executions')
        known_tools = {r[0] for r in cur.fetchall()}
        # Get improvement descriptions
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
    # Parse improvements.md
    imp_file = RSI_DIR / 'improvements.md'
    files_to_parse = []
    if imp_file.exists():
        files_to_parse.append(imp_file)
    # Parse history files
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
        # Each entry starts with **Date:** and contains target file/tool info
        entries = re.split(r'(?=\*\*Date:\*\*)', content)
        for entry in entries:
            if not entry.strip():
                continue
            # Check which tools this improvement mentions (count ALL matches, no break)
            entry_lower = entry.lower()
            for tool in tools_in_db:
                if re.search(r'\b' + re.escape(tool.lower()) + r'\b', entry_lower):
                    counts[tool] += 1
    return dict(counts)


def collect_rsi_highlights():
    highlights = []
    # Parse improvements.md - each entry has **Date:** YYYY-MM-DD HH:MM UTC
    imp_file = RSI_DIR / 'improvements.md'
    if imp_file.exists():
        content = imp_file.read_text()
        # Match individual entries by their Date field
        for m in re.finditer(r'\*\*Date:\*\*\s+(\d{4}-\d{2}-\d{2})', content):
            highlights.append({'date': m.group(1), 'description': ''})
    # Also count history files (each file may contain multiple entries)
    history_dir = RSI_DIR / 'history'
    if history_dir.exists():
        for f in sorted(history_dir.glob('*.md')):
            m = re.match(r'(\d{4}-\d{2}-\d{2})', f.name)
            if m:
                # Count actual entries in the file by Date fields
                hist_content = f.read_text()
                entry_count = len(re.findall(r'\*\*Date:\*\*\s+\d{4}-\d{2}-\d{2}', hist_content))
                if entry_count == 0:
                    entry_count = 1  # fallback: at least 1 entry per file
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
    # Append today's live snapshot to each file's history
    for fname, kb in brain_sizes.items():
        if fname not in brain_history:
            brain_history[fname] = []
        # Always append current snapshot as latest point
        brain_history[fname].append({'date': today_display, 'kb': kb, 'iso': today_iso})
    top_brain = list(brain_sizes.items())[:10]
    other_kb = sum(v for k, v in list(brain_sizes.items())[10:])
    if other_kb > 0:
        top_brain.append(('Other', round(other_kb, 1)))
    brain_labels = [x[0] for x in top_brain]
    brain_kb = [x[1] for x in top_brain]
    # Limit history to top-10 by current size
    top10_files = set(x[0] for x in top_brain) - {'Other'}
    brain_history_data = []
    for fname, points in sorted(brain_history.items(), key=lambda x: x[0]):
        if fname in top10_files:
            brain_history_data.append({'file': fname, 'points': points})
    total_kb = round(sum(brain_sizes.values()), 1)
    return {
        'name': profile_name,
        'brain_labels': brain_labels,
        'brain_kb': brain_kb,
        'brain_history': brain_history_data,
        'total_kb': total_kb,
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
        # x = usage count, y = fail rate, r+color = RSI applied (database events)
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

    # Stacked data for most used: success vs failure + applied/mention rate
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

    # Build profiles JSON for switcher
    profiles_json = json.dumps(profiles_data)
    tool_json = json.dumps(tool_data)

    html = TEMPLATE
    html = html.replace('__GENERATED_AT__', generated_at)
    html = html.replace('__DATA_PERIOD__', data_period)
    html = html.replace('__PROFILES_JSON__', profiles_json)
    html = html.replace('__TOOL_JSON__', tool_json)
    # Default to first profile for summary cards
    default_p = profiles_data[0]
    html = html.replace('__TOTAL_KB__', str(default_p['total_kb']))
    html = html.replace('__FILE_COUNT__', str(default_p['file_count']))
    html = html.replace('__TOTAL_RSI__', str(tool_data['total_rsi']))
    html = html.replace('__MOST_USED__', tool_data['most_used'])
    html = html.replace('__WORST_RATE__', str(tool_data['worst_rate']))
    html = html.replace('__WORST_NAME__', tool_data['worst_name'])
    return html


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RSI/Brain Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root { --bg: #1a1a2e; --card: #16213e; --text: #e0e0e0; --accent: #0f3460; --hi: #e94560; --green: #2ecc71; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; }
  h1 { text-align: center; margin-bottom: 10px; color: var(--hi); }
  .meta { text-align: center; opacity: 0.7; margin-bottom: 20px; font-size: 0.9em; }
  .switcher { display: flex; justify-content: center; gap: 10px; margin-bottom: 25px; }
  .switcher button { background: var(--card); color: var(--text); border: 1px solid var(--accent); padding: 8px 20px; border-radius: 6px; cursor: pointer; font-size: 0.95em; transition: all 0.2s; }
  .switcher button.active { background: var(--hi); border-color: var(--hi); color: #fff; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin-bottom: 30px; }
  .card { background: var(--card); padding: 20px; border-radius: 8px; text-align: center; }
  .card .value { font-size: 1.8em; font-weight: bold; color: var(--hi); }
  .card .label { opacity: 0.8; margin-top: 5px; font-size: 0.85em; }
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; }
  .chart-container { background: var(--card); padding: 20px; border-radius: 8px; cursor: pointer; transition: all 0.3s; }
  .chart-container.expanded { grid-column: 1 / -1; max-height: none; }
  .chart-container.expanded canvas { max-height: 600px; }
  .chart-container h3 { margin-bottom: 15px; color: var(--hi); font-size: 1em; user-select: none; display: flex; justify-content: space-between; align-items: center; }
  .chart-container h3 { position: relative; padding-right: 30px; }
  .chart-container h3::after { content: '⛶'; font-size: 0.9em; opacity: 0.6; position: absolute; right: 0; top: 0; }
  .chart-container.expanded h3::after { content: '✕'; }
  .list-panel { background: var(--card); padding: 20px; border-radius: 8px; cursor: pointer; transition: all 0.3s; }
  .list-panel.expanded { grid-column: 1 / -1; max-height: none; overflow-y: visible; }
  .list-panel h3 { margin-bottom: 15px; color: var(--hi); font-size: 1em; user-select: none; display: flex; justify-content: space-between; align-items: center; position: relative; padding-right: 30px; }
  .list-panel h3::after { content: '⛶'; font-size: 0.9em; opacity: 0.6; position: absolute; right: 0; top: 0; }
  .list-panel.expanded h3::after { content: '✕'; }
  .list-panel table { width: 100%; border-collapse: collapse; }
  .list-panel th, .list-panel td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--accent); }
  .list-panel th { color: var(--hi); font-size: 0.85em; text-transform: uppercase; }
  .list-panel td { font-size: 0.9em; }
  .section-title { font-size: 1.3em; color: var(--hi); margin: 30px 0 15px; padding-bottom: 8px; border-bottom: 1px solid var(--accent); }
  canvas { max-height: 300px; }
  @media (max-width: 600px) { .charts { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<h1>🧠 RSI / Brain Analytics</h1>
<div class="meta">Generated: __GENERATED_AT__ | Data Period: __DATA_PERIOD__</div>

<div class="section-title">🧠 Profile Analytics</div>
<div class="switcher" id="profileSwitcher"></div>

<div class="cards">
  <div class="card"><div class="value" id="cardKB">__TOTAL_KB__ KB</div><div class="label">Brain Size (<span id="cardFiles">__FILE_COUNT__</span> files)</div></div>
</div>

<div class="charts">
  <div class="chart-container" onclick="toggleExpand(this)">
    <h3>Brain File Sizes (KB) — Top 10 + Others</h3>
    <canvas id="brainChart"></canvas>
  </div>
  <div class="chart-container" onclick="toggleExpand(this)">
    <h3>Brain File Size History (KB)</h3>
    <canvas id="brainHistoryChart"></canvas>
  </div>
</div>

<div class="section-title">🛠️ Global Tool & RSI Analytics</div>

<div class="charts">
  <div class="chart-container" onclick="toggleExpand(this)">
    <h3>Tool Usage vs Failure Rate (bubble size & color = RSI applied)</h3>
    <div id="scatterLegend" style="display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:0.8em;opacity:0.8;">
      <span>Applied:</span>
      <span>0</span>
      <div style="width:150px;height:12px;border-radius:3px;background:linear-gradient(to right,#2ecc71,#f1c40f,#e74c3c);"></div>
      <span>20+</span>
    </div>
    <canvas id="scatterChart"></canvas>
  </div>
  <div class="chart-container" onclick="toggleExpand(this)">
    <h3>Most Used Tools (stacked: success/failure + fail rate %)</h3>
    <canvas id="topToolsChart"></canvas>
  </div>
  <div class="list-panel" onclick="toggleExpand(this)">
    <h3>RSI Applied / Mention Rate (Top 10)</h3>
    <table id="appliedTable"><thead><tr><th>Tool</th><th>Applied</th><th>Mentions</th><th>Rate</th></tr></thead><tbody></tbody></table>
  </div>
  <div class="chart-container" onclick="toggleExpand(this)">
    <h3>RSI Improvements Timeline</h3>
    <canvas id="rsiChart"></canvas>
  </div>
  <div class="list-panel" onclick="toggleExpand(this)">
    <h3>Least Used Tools</h3>
    <table id="leastTable"><thead><tr><th>Tool</th><th>Calls</th></tr></thead><tbody></tbody></table>
  </div>
  <div class="list-panel" onclick="toggleExpand(this)">
    <h3>Lowest Failure Rates (min 5 calls)</h3>
    <table id="bestTable"><thead><tr><th>Tool</th><th>Fail Rate</th><th>Calls</th></tr></thead><tbody></tbody></table>
  </div>
</div>

<script>
window.toggleExpand = function(el) {
  el.classList.toggle('expanded');
  window.dispatchEvent(new Event('resize'));
};

var PROFILES = __PROFILES_JSON__;
var TOOLS = __TOOL_JSON__;
var currentProfile = 0;
var charts = {};

var commonOpts = {
  responsive: true,
  plugins: { legend: { labels: { color: '#e0e0e0' } } },
  scales: { x: { ticks: { color: '#aaa' } }, y: { ticks: { color: '#aaa' } } }
};

function initCharts() {
  // Brain sizes bar
  charts.brain = new Chart(document.getElementById('brainChart'), {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'Size (KB)', data: [], backgroundColor: '#e94560' }] },
    options: Object.assign({}, commonOpts, { indexAxis: 'y', plugins: { legend: { display: false } } })
  });

  // Bubble scatter: x=usage, y=fail rate, r+color=RSI
  charts.scatter = new Chart(document.getElementById('scatterChart'), {
    type: 'bubble',
    data: { datasets: [{ label: 'Tools', data: [], backgroundColor: [] }] },
    options: Object.assign({}, commonOpts, {
      scales: {
        x: { title: { display: true, text: 'Total Executions', color: '#aaa' }, ticks: { color: '#aaa' } },
        y: { title: { display: true, text: 'Failure Rate (%)', color: '#aaa' }, ticks: { color: '#aaa' } }
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: function(ctx) {
        return ctx.raw.label + ': ' + ctx.raw.x + ' uses, ' + ctx.raw.y + '% fail, ' + ctx.raw.rsi + ' RSI';
      }}}}
    })
  });

  // Brain history line (time scale)
  charts.history = new Chart(document.getElementById('brainHistoryChart'), {
    type: 'line',
    data: { datasets: [] },
    options: Object.assign({}, commonOpts, {
      scales: {
        x: { type: 'time', time: { unit: 'hour', displayFormats: { hour: 'MM-dd HH:00' } }, ticks: { color: '#aaa', maxRotation: 45 } },
        y: { title: { display: true, text: 'KB', color: '#aaa' }, ticks: { color: '#aaa' } }
      }
    })
  });

  // Top tools stacked
  charts.top = new Chart(document.getElementById('topToolsChart'), {
    type: 'bar',
    data: { labels: [], datasets: [
      { label: 'Success', data: [], backgroundColor: '#2ecc71' },
      { label: 'Failure', data: [], backgroundColor: '#e94560' }
    ]},
    options: Object.assign({}, commonOpts, {
      scales: { x: { stacked: true, ticks: { color: '#aaa' } }, y: { stacked: true, ticks: { color: '#aaa' } } },
      plugins: { legend: { display: true, labels: { color: '#e0e0e0' } },
        tooltip: { callbacks: { afterBody: function(items) {
          var idx = items[0].dataIndex;
          return 'Fail rate: ' + TOOLS.top_fail_rates[idx] + '%';
        }}}
      }
    })
  });

  // RSI timeline
  charts.rsi = new Chart(document.getElementById('rsiChart'), {
    type: 'line',
    data: { labels: [], datasets: [{ label: 'Cumulative Improvements', data: [], borderColor: '#e94560', fill: false, tension: 0.3 }] },
    options: Object.assign({}, commonOpts, { plugins: { legend: { display: false } } })
  });
}

function updateProfile(idx) {
  currentProfile = idx;
  var p = PROFILES[idx];

  // Update cards
  document.getElementById('cardKB').textContent = p.total_kb + ' KB';
  document.getElementById('cardFiles').textContent = p.file_count;

  // Update switcher buttons
  var btns = document.querySelectorAll('.switcher button');
  btns.forEach(function(b, i) { b.classList.toggle('active', i === idx); });

  // Brain sizes
  charts.brain.data.labels = p.brain_labels;
  charts.brain.data.datasets[0].data = p.brain_kb;
  charts.brain.update();

  // Brain history
  var colors = ['#e94560','#0f3460','#2ecc71','#f39c12','#9b59b6','#1abc9c','#e67e22','#3498db','#e74c3c','#2c3e50'];
  var datasets = [];
  p.brain_history.forEach(function(fh, i) {
    datasets.push({
      label: fh.file,
      data: fh.points.map(function(pt) {
        // Use pre-computed ISO timestamp for proper time-scale positioning
        return { x: pt.iso, y: pt.kb };
      }),
      borderColor: colors[i % colors.length],
      fill: false,
      tension: 0.3
    });
  });
  charts.history.data.datasets = datasets;
  charts.history.update();
}

function rsiColor(rsi) {
  // Green (0) -> Yellow -> Red (high)
  if (rsi === 0) return 'rgba(46,204,113,0.7)';
  var t = Math.min(rsi / 20, 1);
  var r = Math.round(46 + t * (233 - 46));
  var g = Math.round(204 - t * 204);
  var b = Math.round(113 - t * 113);
  return 'rgba(' + r + ',' + g + ',' + b + ',0.8)';
}

function initToolCharts() {
  // Scatter with color scale
  var bgColors = TOOLS.scatter.map(function(d) { return rsiColor(d.rsi); });
  charts.scatter.data.datasets[0].data = TOOLS.scatter;
  charts.scatter.data.datasets[0].backgroundColor = bgColors;
  charts.scatter.update();

  // Top tools stacked
  charts.top.data.labels = TOOLS.top_labels;
  charts.top.data.datasets[0].data = TOOLS.top_success;
  charts.top.data.datasets[1].data = TOOLS.top_fails;
  charts.top.update();

  // Least used list
  var leastTbody = document.querySelector('#leastTable tbody');
  leastTbody.innerHTML = '';
  TOOLS.least_list.forEach(function(item) {
    var tr = document.createElement('tr');
    tr.innerHTML = '<td>' + item.name + '</td><td>' + item.count + '</td>';
    leastTbody.appendChild(tr);
  });

  // Best tools list
  var bestTbody = document.querySelector('#bestTable tbody');
  bestTbody.innerHTML = '';
  TOOLS.best_list.forEach(function(item) {
    var tr = document.createElement('tr');
    tr.innerHTML = '<td>' + item.name + '</td><td>' + item.rate + '%</td><td>' + item.count + '</td>';
    bestTbody.appendChild(tr);
  });

  // Applied/mention rate table for top 10
  var appliedTbody = document.querySelector('#appliedTable tbody');
  appliedTbody.innerHTML = '';
  TOOLS.top_labels.forEach(function(name, i) {
    var applied = TOOLS.top_applied[i];
    var mentions = TOOLS.top_mentions[i];
    var rate = mentions > 0 ? (applied / mentions * 100).toFixed(0) + '%' : '—';
    var tr = document.createElement('tr');
    tr.innerHTML = '<td>' + name + '</td><td>' + applied + '</td><td>' + mentions + '</td><td>' + rate + '</td>';
    appliedTbody.appendChild(tr);
  });

  // RSI
  charts.rsi.data.labels = TOOLS.rsi_dates;
  charts.rsi.data.datasets[0].data = TOOLS.rsi_cum;
  charts.rsi.update();
}

function buildSwitcher() {
  var container = document.getElementById('profileSwitcher');
  PROFILES.forEach(function(p, i) {
    var btn = document.createElement('button');
    btn.textContent = p.name;
    btn.onclick = function() { updateProfile(i); };
    if (i === 0) btn.classList.add('active');
    container.appendChild(btn);
  });
}

initCharts();
buildSwitcher();
updateProfile(0);
initToolCharts();
</script>
</body>
</html>
"""


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
