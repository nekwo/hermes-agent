"""Reproduce the pinned, read-only history inventory. No checkout/ref mutations."""
import collections
import csv
import json
import pathlib
import re
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[3]
FORK = '34ad8ba33f2508ab10bb24a26f0377ddb62660cb'
UPSTREAM = '110baa095bc7135a0624557a9cc35df0f98ece0f'

def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args]).decode('utf-8')

def group(path):
    p = path.lower().replace('\\', '/')
    if 'local_llama' in p or 'local-llama' in p: return '14-local-llama'
    if 'charsheet' in p or '/pet/' in p: return '12-character-assets'
    if p.startswith('mobile_core/'): return '13-mobile'
    if p.startswith('docs/') or p.endswith('.md'): return '15-documentation'
    if any(s in p for s in ['realm', 'sync_', '_sync', 'peer_directory', 'level_sync']): return '09-realm-sync'
    if any(s in p for s in ['office', 'board', 'flow_graph', 'flow_commands']): return '08-office-board-graph'
    if any(s in p for s in ['skill', 'mcp', 'toolset', 'tool_visibility', 'tool_permissions']): return '05-skills-tools-mcp'
    if any(s in p for s in ['provider', 'auth', 'credential', 'codex', 'config', 'readiness']): return '03-auth-config-provider'
    if any(s in p for s in ['serve', 'gateway', 'pairing', 'socket', 'tls']): return '06-transport'
    if any(s in p for s in ['snapshot', 'stream', 'patch', 'observab', 'timeline', 'build_identity']): return '07-projections-observability'
    if any(s in p for s in ['persona', 'instance', 'profile', 'agent_create', 'agent_retire']): return '04-persona-lifecycle'
    if any(s in p for s in ['chat', 'turn', 'dispatch', 'continuity', 'compression', 'prompt', 'auxiliary']): return '10-conversation'
    if p.startswith('agent_runtime/') or p.startswith('tests/agent_runtime/'): return '02-runtime-storage'
    if p.startswith('tools/') or p.startswith('tests/tools/') or p.startswith('agent/') or p == 'run_agent.py': return '11-core-tool-seams'
    if p.startswith(('scripts/', '.github/', '.githooks/', 'tests/')) or p in ['pyproject.toml', 'uv.lock', '.gitattributes', '.gitignore']: return '01-build-test-platform'
    return '00-needs-manual-split'

base = git('merge-base', FORK, UPSTREAM).strip()
raw = git('log', '--reverse', '--topo-order', '--format=%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%B%x1e', f'{UPSTREAM}..{FORK}')
commits = []
for record in raw.split('\x1e'):
    if not record.strip(): continue
    sha, parents, author, email, date, message = record.strip().split('\x1f', 5)
    commits.append(dict(sha=sha, parents=parents.split(), author=author, email=email, date=date, message=message.strip(),
                        coauthors=re.findall(r'(?im)^co-authored-by:\s*(.*)$', message)))
# Diff against first parent includes merge-resolution effects; it does not assign
# every merged patch to the merge author or imply side-branch work is disposable.
paths_by_commit = {}
for c in commits:
    paths = git('diff', '--name-only', '--no-renames', c['parents'][0], c['sha']).splitlines()
    c['paths'] = paths
    c['review_groups'] = sorted({group(p) for p in paths})
    paths_by_commit[c['sha']] = paths
net = git('diff', '--name-status', '--no-renames', base, FORK).splitlines()
up_paths = set(git('ls-tree', '-r', '--name-only', UPSTREAM).splitlines())
base_paths = set(git('ls-tree', '-r', '--name-only', base).splitlines())
rows = []
for line in net:
    status, path = line.split('\t', 1)
    rows.append(dict(path=path, status=status, group=group(path), existed_at_base=path in base_paths,
                     exists_upstream=path in up_paths))
(HERE/'commits.json').write_text(json.dumps(commits, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
with (HERE/'original-to-review-groups.csv').open('w', newline='', encoding='utf-8') as f:
    w = csv.writer(f); w.writerow(['original_sha','parents','author','email','coauthors','review_groups','consolidated_sha','subject'])
    for c in commits:
        w.writerow([c['sha'], ' '.join(c['parents']), c['author'], c['email'], '; '.join(c['coauthors']),
                    ';'.join(c['review_groups']), 'NOT_CONSOLIDATED', c['message'].splitlines()[0]])
with (HERE/'net-paths.csv').open('w', newline='', encoding='utf-8') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
summary = dict(fork=FORK, upstream=UPSTREAM, merge_base=base, fork_tree=git('rev-parse', FORK+'^{tree}').strip(),
    counts=git('rev-list','--left-right','--count',FORK+'...'+UPSTREAM).strip(),
    commits=len(commits), merges=sum(len(c['parents'])>1 for c in commits), net_paths=len(rows),
    net_paths_absent_upstream=sum(not r['exists_upstream'] for r in rows),
    newly_added_paths_absent_upstream=sum(r['status']=='A' and not r['exists_upstream'] for r in rows),
    shared_changed_paths=sum(r['exists_upstream'] for r in rows),
    groups=dict(collections.Counter(r['group'] for r in rows)),
    authors=dict(collections.Counter(c['author']+' <'+c['email']+'>' for c in commits)),
    coauthors=sorted({a for c in commits for a in c['coauthors']}))
(HERE/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary,indent=2))

worktrees = []
for repo in ['X:/Eternia/hermes-agent', 'X:/Unreal Engine/Engine/Launcher/EterniaLauncher']:
    lines = subprocess.check_output(['git', '-C', repo, 'worktree', 'list', '--porcelain'], text=True).splitlines()
    for line in lines:
        if not line.startswith('worktree '): continue
        path = line[9:]
        if pathlib.Path(path).resolve() == ROOT.resolve(): continue
        worktrees.append(dict(repo=repo, path=path,
            head=subprocess.check_output(['git','-C',path,'rev-parse','HEAD'],text=True).strip(),
            status=subprocess.check_output(['git','-C',path,'status','--porcelain=v1'],text=True).splitlines()))
(HERE/'worktree-baseline.json').write_text(json.dumps(worktrees,indent=2)+'\n',encoding='utf-8')
