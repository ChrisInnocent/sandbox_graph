"""A deliberately small, non-executable grammar for sourced installer data."""
import re

FORBIDDEN={'BASH_ENV','ENV','PATH','HOME','IFS','SHELLOPTS','BASHOPTS','CDPATH','GLOBIGNORE',
    'CUBE_SANDBOX_INSTALL_ROOT','ONE_CLICK_ENV_FILE','ONE_CLICK_PACKAGE_TAR','ONE_CLICK_DEPLOY_ROLE'}


def parse_literal_env(payload):
    if not isinstance(payload,bytes) or len(payload)>1024*1024:raise ValueError('bounded literal environment required')
    result={}
    for line in payload.decode('utf-8').splitlines():
        if not line or line.startswith('#'):continue
        matched=re.fullmatch(r"([A-Z_][A-Z0-9_]*)='([^'\r\n\x00]*)'",line)
        if not matched:raise ValueError('environment must contain only single-quoted literal assignments')
        key,value=matched.groups()
        if key in FORBIDDEN or key.startswith(('LD_','DYLD_','BASH_')) or key in result:
            raise ValueError('environment contains duplicate or execution-control assignment')
        result[key]=value
    return result


def render_literal_env(values):
    if not isinstance(values,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in values.items()):
        raise ValueError('literal string settings required')
    data=''.join(f"{key}='{value}'\n" for key,value in sorted(values.items())).encode('utf-8')
    if parse_literal_env(data)!=values:raise ValueError('unrepresentable literal environment')
    return data
