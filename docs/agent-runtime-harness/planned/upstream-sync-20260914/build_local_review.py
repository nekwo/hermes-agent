"""Build a separate three-commit Local llama review series; never change main."""
import json
import os
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[3]
BRANCH = 'refs/heads/codex/local-llama-review-20260914'

def git(*args, **kwargs):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], **kwargs).decode().strip()

original = git('rev-parse','34ad8ba33f')
parent = git('rev-parse','5430b7840a')
groups = [
    ('9015185e4e', 'docs(local-llama): define Hermes-owned console lifecycle contract'),
    ('31bf01536a', 'feat(local-llama): implement owned runtime, guarded RPC and isolated inference'),
    ('34ad8ba33f', 'docs(local-llama): propose installer contracts and crash recovery'),
]
if subprocess.run(['git','-C',str(ROOT),'show-ref','--verify','--quiet',BRANCH]).returncode == 0:
    raise SystemExit('Review ref already exists; refuse to replace it')
mapping=[]
original_parent=parent
for tip, title in groups:
    tip=git('rev-parse',tip)
    sources=git('rev-list','--reverse',original_parent+'..'+tip).splitlines()
    credit=[]
    for source in sources:
        identity=git('show','-s','--format=%an <%ae>',source)
        credit.append((source,identity))
    meta=git('show','-s','--format=%an%n%ae%n%aI',sources[0]).splitlines()
    environment=dict(os.environ,GIT_AUTHOR_NAME=meta[0],GIT_AUTHOR_EMAIL=meta[1],GIT_AUTHOR_DATE=meta[2])
    body=title+'\n\nReview-only consolidation. Do not use this branch to replace published main.\n'
    body+='Snapshot tree copied exactly from '+tip+'.\nOriginal commits and attribution:\n'
    body+=''.join(s+' '+a+'\n' for s,a in credit)
    trailers=set()
    for source in sources:
        for line in git('show','-s','--format=%B',source).splitlines():
            if line.lower().startswith('co-authored-by:'): trailers.add(line)
    body+='\n'+'\n'.join(sorted(trailers))+'\n'
    tree=git('rev-parse',tip+'^{tree}')
    result=git('commit-tree',tree,'-p',parent,input=body.encode(),env=environment)
    mapping.append(dict(consolidated=result, original_commits=sources, original_tip=tip, tree=tree, title=title))
    parent=result; original_parent=tip
assert git('rev-parse',parent+'^{tree}') == git('rev-parse',original+'^{tree}')
subprocess.run(['git','-C',str(ROOT),'diff','--exit-code',original,parent],check=True)
git('update-ref',BRANCH,parent,'0'*40)
(HERE/'local-llama-consolidation.json').write_text(json.dumps(dict(branch=BRANCH,tip=parent,
    original=original,tree=git('rev-parse',parent+'^{tree}'),tree_equivalence=True,
    unchanged_ancestry_through=git('rev-parse','5430b7840a'),groups=mapping),indent=2)+'\n')
print(parent)
