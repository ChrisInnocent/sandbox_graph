"""方法目录与能力证据分离；目录不是可运行或environment_verified证明。"""
METHODS = {
    'B00.TEMPLATE_BUILD': 'build', 'B00.SNAPSHOT_BUILD': 'build_snapshot',
    'B01.TEMPLATE': 'create', 'B01.SNAPSHOT.DISK': 'create', 'B01.SNAPSHOT.MEMORY': 'create',
    'B02.FULL': 'image', 'B02.PREPARED': 'create',
    'B03.PAUSE': 'pause', 'B04.MEMORY': 'resume', 'B04.DISK': 'resume',
    'B05.FULL': 'clone', 'B05.PREPARED': 'clone', 'B05.NATIVE': 'fork',
    'B06.RUNNING': 'delete', 'B06.PAUSED': 'delete',
    'B07.FIRST': 'command', 'B07.STEADY': 'command', 'B07.OUTPUT': 'command', 'B07.ERROR': 'command',
    'B08.LOCAL': 'snapshot', 'B08.PUBLISHED': 'snapshot',
    'B09.PLATFORM': 'noise', 'B09.IDLE': 'noise', 'B09.PAUSED': 'noise',
    'B10.GROWTH_LIST': 'density', 'B10.GROWTH_READY': 'density',
    'B10.PROJECTION': 'projection', 'B10.SLO_VERIFY': 'slo',
    'B10.FIXED_REQUESTS.WINDOW': 'density', 'B10.FIXED_REQUESTS.BATCH': 'density',
    'E01.WORKLOAD': 'extension', 'E02.RESET': 'extension', 'E03.MIXED_SOAK': 'extension',
    'E04.PRESSURE': 'extension', 'E05.INTERFERENCE': 'extension', 'E06.RECOVERY': 'extension',
}


from .creation_hold import HOLD_METHODS, base_method
METHODS.update({method:METHODS[base_method(method)] for method in HOLD_METHODS})

def capability(product, method):
    if method not in METHODS:
        raise ValueError('unknown method')
    status = 'source_audited' if product in ('kuasar', 'cubesandbox', 'agentenv') else 'unverified'
    if method.startswith('E'):
        status = 'unverified'
    if product == 'kuasar' and method == 'B05.NATIVE':
        status = 'unsupported'
    return {'product': product, 'method_id': method, 'status': status,
            'implementation_status': 'unverified', 'environment_verified': False,
            'evidence_ref': f'docs/source-audit/{product}.md' if status != 'unverified' else None}
