"""Load a private, locally installed recipe catalog; no catalog arrives on SSH stdin."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
from .products.common import RecipeStep
from .declarations import _unique_json_object
from ..domain.models import canonical_hash


def load_recipe_catalog(path='/etc/sandbox-graph/recipes.json'):
    path=Path(path)
    if not path.is_file() or path.resolve()!=path.absolute():raise ValueError('recipe catalog must be a regular canonical file')
    info=path.stat()
    if info.st_uid not in {0,os.geteuid()} or stat.S_IMODE(info.st_mode)&0o022:
        raise PermissionError('recipe catalog is writable by another identity')
    if info.st_size>4*1024*1024:raise ValueError('recipe catalog exceeds byte budget')
    data=json.loads(path.read_bytes(),object_pairs_hook=_unique_json_object)
    if set(data)!={'schema_version','recipes'} or data['schema_version']!=1 or not isinstance(data['recipes'],dict):
        raise ValueError('invalid recipe catalog')
    result={}
    for name,value in data['recipes'].items():
        if not isinstance(name,str) or not name or set(value)!={'digest','steps'}:raise ValueError('invalid recipe registration')
        steps=tuple(RecipeStep(**(row|{'argv':tuple(row['argv'])})) for row in value['steps'])
        if not steps or len(steps)>256 or len({step.step_id for step in steps})!=len(steps):raise ValueError('bounded unique recipe steps required')
        if any(step.kind not in {'file','archive','command','verify','dependency-lock','kuasar-tenant','kuasar-groups','kuasar-group-config','cube-compose','owned-container','docker-image','ublk-limit'} or not 0<step.timeout_s<=3600 for step in steps):
            raise ValueError('unregistered recipe primitive or timeout')
        if canonical_hash([asdict(step) for step in steps])!=value['digest']:raise ValueError('recipe catalog digest differs')
        result[name]=steps
    return result
