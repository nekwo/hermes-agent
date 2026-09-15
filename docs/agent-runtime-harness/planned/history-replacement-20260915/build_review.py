"""Build a same-tree review history with a private index; never move main.

Run from the repository root. Outputs include full historical provenance, not
an assertion that every historical patch still survives in the final source.
"""
import ast
import collections
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OLD = 'c112a9347a4e14ce573cdbf3d154177a6f521843'
BASE = '110baa095bc7135a0624557a9cc35df0f98ece0f'
REF = 'refs/heads/codex/compact-main-final-20260915'


def git(*args, data=None, env=None):
    return subprocess.check_output(
        ['git', '-c', 'core.hooksPath=NUL', '-C', str(ROOT), *args],
        input=data, env=env,
    )


# Reuse only the classifier, never execute the old inventory's write actions.
module = ast.parse((HERE.parent / 'upstream-sync-20260914/inventory.py').read_text(encoding='utf-8'))
function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'group')
namespace = {}
exec(compile(ast.Module(body=[function], type_ignores=[]), 'classifier', 'exec'), namespace)


OVERRIDES = {'tests/hermes_state/test_append_messages_batch.py': '02-runtime-storage', 'tests/hermes_state/test_retired_wal_generation_capture.py': '02-runtime-storage', 'tests/hermes_state/test_session_db_read_conn_pool.py': '02-runtime-storage', 'tests/hermes_state/test_downstream_session_contracts.py': '02-runtime-storage', 'tests/agent/test_anthropic_keychain.py': '03-auth-config-provider', 'tests/agent/test_anthropic_spent_rotation_verdict.py': '03-auth-config-provider', 'plugins/dashboard_auth/_shared.py': '03-auth-config-provider', 'tests/plugins/dashboard_auth/test_nous_provider.py': '03-auth-config-provider', 'tests/plugins/dashboard_auth/test_self_hosted_provider.py': '03-auth-config-provider', 'tests/hermes_cli/test_dashboard_auth_gate.py': '03-auth-config-provider', 'hermes_cli/plugins.py': '05-skills-tools-mcp', 'hermes_cli/plugins_discovery.py': '05-skills-tools-mcp', 'tests/hermes_cli/test_plugins.py': '05-skills-tools-mcp', 'tests/plugins/memory/test_holographic_store.py': '05-skills-tools-mcp', 'tests/agent/test_anthropic_adapter.py': '11-core-tool-seams', 'tests/agent/test_image_routing.py': '11-core-tool-seams', 'tests/agent/test_shell_hooks.py': '11-core-tool-seams', 'tests/agent/test_shell_hooks_consent.py': '11-core-tool-seams', 'tests/agent/test_shared_ssl_context.py': '11-core-tool-seams', 'tests/agent/test_usage_pricing.py': '11-core-tool-seams', 'tests/hermes_cli/test_harness_characters_cli.py': '12-character-assets', 'tests/hermes_cli/test_harness_characters_draftsman_seam.py': '12-character-assets', 'tests/hermes_cli/test_harness_pets_cli.py': '12-character-assets', 'tests/agent/test_pet_generate.py': '12-character-assets', 'tests/docker/test_dashboard.py': '01-build-test-platform', 'tests/hermes_cli/test_dashboard_tui_backcompat.py': '01-build-test-platform', 'tests/hermes_cli/test_dashboard_unified_launch.py': '01-build-test-platform', 'tests/hermes_cli/test_update_stale_dashboard.py': '01-build-test-platform'}


def group(path):
    if path in OVERRIDES:
        return OVERRIDES[path]
    p = path.lower()
    if p.startswith('contributors/'):
        return '15-documentation'
    if 'kanban' in p or p.endswith('/level.py'):
        return '08-office-board-graph'
    if p.startswith(('hermes_state', 'hermes_cli/harness')):
        return '02-runtime-storage'
    if p.startswith('plugins/') or p == 'model_tools.py':
        return '05-skills-tools-mcp'
    assigned = namespace['group'](path)
    if assigned != '00-needs-manual-split':
        return assigned
    if p.startswith(('apps/', 'hermes_cli/')) or p in ('cli.py', 'hermes_constants.py', 'utils.py'):
        return '01-build-test-platform'
    if p.startswith(('evals/', 'nix/', 'qa_artifacts/', 'qa-artifacts/', 'tool/')) or p in ('.launcher_run.log', 'htasks_open.json'):
        return '01-build-test-platform'
    raise ValueError(f'Unclassified path: {path}')


def entries(ref):
    result = {}
    for row in git('ls-tree', '-r', '-z', ref).split(b'\0'):
        if row:
            header, path = row.split(b'\t', 1)
            result[path] = header
    return result


def main():
    baseline, final = entries(BASE), entries(OLD)
    changed = sorted(p for p in baseline.keys() | final.keys() if baseline.get(p) != final.get(p))
    groups = collections.defaultdict(list)
    for path in changed:
        groups[group(path.decode('utf-8'))].append(path)
    commits = []
    cached_file = HERE / 'original-commits.json'
    cached_paths = {c['sha']: c['paths'] for c in json.loads(cached_file.read_text(encoding='utf-8'))} if cached_file.exists() else {}
    log = git('log', '--reverse', '--topo-order', '--format=%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%B%x1e', f'{BASE}..{OLD}').decode('utf-8')
    authors = collections.defaultdict(set)
    path_groups = {p.decode('utf-8'): g for g, ps in groups.items() for p in ps}
    for record in log.split('\x1e'):
        if not record.strip():
            continue
        sha, parents, author, email, date, message = record.strip().split('\x1f', 5)
        paths = cached_paths.get(sha)
        if paths is None:
            paths = git('diff', '--name-only', '-z', '--no-renames', parents.split()[0], sha).decode('utf-8').strip('\0').split('\0')
        themes = sorted({path_groups[p] for p in paths if p in path_groups})
        coauthors = re.findall(r'(?im)^co-authored-by:\s*(.+)$', message)
        for theme in themes:
            authors[theme].update([f'{author} <{email}>', *coauthors])
        commits.append(dict(sha=sha, parents=parents.split(), author=author, email=email, date=date,
                            message=message.strip(), coauthors=coauthors, paths=paths, groups=themes))
    mapping = {}
    with tempfile.TemporaryDirectory(prefix='hermes-history-index-') as temporary:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        git('read-tree', BASE, env=env)
        parent = BASE
        order = ['01-build-test-platform', '02-runtime-storage', '03-auth-config-provider',
                 '11-core-tool-seams', '05-skills-tools-mcp', '04-persona-lifecycle',
                 '10-conversation', '14-local-llama', '06-transport',
                 '07-projections-observability', '08-office-board-graph', '09-realm-sync',
                 '12-character-assets', '13-mobile', '15-documentation']
        assert set(order) == set(groups)
        for theme in order:
            paths = groups[theme]
            updates = b''.join((final[p] if p in final else b'0 ' + b'0' * 40) + b'\t' + p + b'\0' for p in paths)
            git('update-index', '-z', '--index-info', data=updates, env=env)
            tree = git('write-tree', env=env).decode().strip()
            message = (f'fork: {theme[3:].replace("-", " ")}\n\n'
                       f'Same-tree reconstruction of {OLD}.\n'
                       f'Pinned upstream: {BASE}.\n'
                       'Historical commits remain on codex/pre-history-replacement-20260915.\n'
                       'Groups are source review checkpoints; dependent groups land together.\n'
                       'Credit below records contributors to these paths across the archived history;\n'
                       'it does not claim every historical patch survives in the final tree.\n\n'
                       + ''.join(f'Co-authored-by: {a.strip()}\n' for a in sorted(authors[theme])))
            parent = git('commit-tree', tree, '-p', parent, data=message.encode('utf-8'), env=env).decode().strip()
            mapping[theme] = parent
    original_tree = git('rev-parse', OLD + '^{tree}').decode().strip()
    replacement_tree = git('rev-parse', parent + '^{tree}').decode().strip()
    assert original_tree == replacement_tree
    assert not git('diff', '--raw', OLD, parent)
    assert int(git('rev-list', '--count', f'{BASE}..{parent}')) == len(groups)
    git('update-ref', REF, parent, '0' * 40)
    for commit in commits:
        commit['consolidated_commits'] = [mapping[g] for g in commit['groups']]
        commit['mapping_kind'] = 'historical-path-provenance' if commit['groups'] else 'archive-only-no-surviving-net-path'
    (HERE / 'original-commits.json').write_text(json.dumps(commits, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    with (HERE / 'path-groups.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['path', 'group', 'consolidated_sha'])
        writer.writerows((p.decode('utf-8'), g, mapping[g]) for g, ps in sorted(groups.items()) for p in ps)
    with (HERE / 'original-to-consolidated.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['original_sha', 'author', 'email', 'groups', 'consolidated_shas', 'mapping_kind', 'subject'])
        writer.writerows((c['sha'], c['author'], c['email'], ';'.join(c['groups']), ';'.join(c['consolidated_commits']), c['mapping_kind'], c['message'].splitlines()[0]) for c in commits)
    evidence = dict(original=OLD, upstream=BASE, replacement=parent, original_tree=original_tree,
                    replacement_tree=replacement_tree, changed_paths=len(changed), original_commits=len(commits),
                    commit_order=order, groups={g: dict(commit=mapping[g], paths=len(ps)) for g, ps in sorted(groups.items())})
    (HERE / 'tree-proof.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
