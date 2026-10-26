"""Closed installation primitives; user input cannot supply an executable command."""
from dataclasses import dataclass,field,replace
from pathlib import PurePosixPath
import re


def absolute(value):
    if not isinstance(value,str) or not value.startswith('/') or '\x00' in value or any(x in ('..','.') for x in value.split('/')):
        raise ValueError('canonical absolute product path required')
    return value


@dataclass(frozen=True)
class RecipeStep:
    step_id: str
    kind: str
    argv: tuple[str,...]
    asset: dict|None=None
    target: str|None=None
    mode: int=0o755
    environment: dict=field(default_factory=dict)
    network: str='host'
    timeout_s: int=300


def require_asset(assets,name):
    item=assets.get(name)
    if not isinstance(item,dict) or set(item)!={'path','sha256'}:
        raise ValueError('digest-locked asset required: '+name)
    absolute(item['path'])
    if not re.fullmatch('[a-f0-9]{64}',item['sha256']):raise ValueError('invalid asset digest')
    return dict(item)


def file_step(assets,name,target,*,mode=0o755):
    asset=require_asset(assets,name);absolute(target)
    return RecipeStep('file:'+name,'file',('install','-m',oct(mode)[2:],asset['path'],target),asset,target,mode)


def archive_step(assets,name,target):
    asset=require_asset(assets,name);absolute(target)
    return RecipeStep('archive:'+name,'archive',(),asset,target)


def command(name,*argv,environment=None,network='host',timeout_s=300):
    return RecipeStep(name,'command',tuple(argv),environment=environment or {},network=network,timeout_s=timeout_s)


def installation_state(state,upgrade):
    if state not in {'absent','installed','partial','unknown'}:raise ValueError('unknown installation state')
    if state in {'partial','unknown'}:raise ValueError('reconcile partial or unknown installation before new install')
    if upgrade and state!='installed':raise ValueError('upgrade requires a verified existing installation')
    return state=='installed' and not upgrade


def roles_exact(roles,allowed):
    if not roles or len(set(roles))!=len(roles) or set(roles)-set(allowed):raise ValueError('unsupported product roles')


def pinned_image(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[^\s@]+@)?sha256:[0-9a-f]{64}',value):
        raise ValueError('container image must have an immutable sha256 digest')
    return value


def missing_steps(recipe,observations,*,dependencies=None,replay_safe=()):
    """Resume confirmed missing primitives without repeating whole installers.

    Observations are a frozen current probe / original step-journal map. Unknown
    effects and non-idempotent setup commands must retain their original action.
    """
    if not isinstance(observations,dict) or set(observations)!={step.step_id for step in recipe}:
        raise ValueError('complete original recipe observations required for partial installation')
    satisfied={'satisfied','matched','running','completed'}
    dependencies=dependencies or {}
    positions={step.step_id:index for index,step in enumerate(recipe)}
    for target,sources in dependencies.items():
        if target not in positions or any(source not in positions or positions[source]>=positions[target] for source in sources):
            raise ValueError('repair dependencies must follow the registered recipe order')
    needed=set()
    for step in recipe:
        state=observations[step.step_id]
        if state in satisfied:continue
        if state not in {'missing','stopped'}:raise ValueError('partial installation has an unresolved or mismatched primitive: '+step.step_id)
        needed.add(step.step_id)
    selected=[]
    for step in recipe:
        if needed.intersection(dependencies.get(step.step_id,())):needed.add(step.step_id)
        if step.step_id not in needed:continue
        safe=(step.kind in {'file','archive','verify','dependency-lock','cube-compose','owned-container','docker-image'} or
            step.kind=='command' and (step.argv[:2] in {('systemctl','start'),('systemctl','daemon-reload')}
                or step.step_id.startswith('validate:') or step.step_id in replay_safe))
        if not safe:raise ValueError('partial installation requires original action reconciliation: '+step.step_id)
        if step.argv[:2]==('systemctl','start') and observations[step.step_id] in satisfied:
            step=replace(step,argv=('systemctl','restart',*step.argv[2:]))
        selected.append(step)
    if any(step.kind=='file' and step.target.endswith('.service') for step in selected):
        reloads=[step for step in recipe if step.argv==('systemctl','daemon-reload')]
        if not reloads:raise ValueError('unit repair requires a registered daemon reload')
        selected=[step for step in selected if step.argv!=('systemctl','daemon-reload')]
        pivot=next((index for index,step in enumerate(selected) if step.argv[:2] in {('systemctl','start'),('systemctl','restart')}),len(selected))
        selected[pivot:pivot]=reloads
    return tuple(selected)


def image_load_step(assets,name,image):
    return RecipeStep('image:'+name,'docker-image',(),asset=require_asset(assets,'image:'+name),
        target=pinned_image(image),timeout_s=360)
