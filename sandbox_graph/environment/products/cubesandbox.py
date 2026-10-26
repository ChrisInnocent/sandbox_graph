"""Cube one-click profile and effective scheduler semantics from audited source."""
import math
from .common import (absolute,archive_step,command,file_step,RecipeStep,installation_state,require_asset,roles_exact)

AUTH_HEADER='X-API-Key'
ROLE_COMPONENTS={'control':('CubeOps','CubeMaster','CubeAPI','cubeproxy','CubeTemplateCenter'),
    'compute':('cubelet','cube-shim')}


def auth_mode(*,callback_url,static_key_present):
    return 'callback' if callback_url else 'static' if static_key_present else 'unprotected'


def effective_limits(*,reported_max,configured_max,reserve_percent,reported_concurrency,
                     configured_concurrency,quota_mem_mib,reserved_mem_mib,api_rate):
    values=(reported_max,configured_max,reported_concurrency,configured_concurrency,quota_mem_mib,reserved_mem_mib,api_rate)
    if any(type(x) is not int or x<0 for x in values) or not 0<=reserve_percent<=1:
        raise ValueError('nonnegative numeric configuration required')
    maximum=(reported_max if reported_max>0 else configured_max) or 3000
    reserved=math.ceil(quota_mem_mib*.1) if quota_mem_mib>0 and reserved_mem_mib>=quota_mem_mib else reserved_mem_mib
    return {'max_sandboxes':maximum,'admission_sandboxes':math.ceil(maximum*reserve_percent),
        'create_concurrency':max(reported_concurrency,configured_concurrency) or 50,
        'reserved_mem_mib':reserved,'api_rate':max(api_rate,1)}


def install_recipe(*,roles,assets,state,root,config_path,upgrade=False,observed_steps=None,control_profile=None):
    roles_exact(roles,ROLE_COMPONENTS)
    if control_profile is not None:
        if tuple(roles)!=('control',):raise ValueError('component control profile requires exactly the control role')
        return control_recipe(assets=assets,state=state,root=root,upgrade=upgrade,observed_steps=observed_steps,**control_profile)
    if state=='partial' and observed_steps is not None:
        from .common import missing_steps
        recipe=install_recipe(roles=roles,assets=assets,state='absent',root=root,config_path=config_path)
        # The upstream install mode resets existing configuration. Its effects
        # cannot be replayed as a missing-file repair or skipped after changing
        # inputs. Reconcile the original install before authorizing a new plan.
        return missing_steps(recipe,observed_steps,
            dependencies={'cube:install':tuple(step.step_id for step in recipe[:-1])})
    if 'control' in roles and 'compute' not in roles:raise ValueError('one-click control includes compute')
    if installation_state(state,upgrade):return ()
    absolute(root);absolute(config_path)
    if root!='/usr/local/services/cubetoolbox':
        raise ValueError('audited one-click installer has a fixed install root')
    bundle=require_asset(assets,'oneclick');package=require_asset(assets,'package')
    dependencies=require_asset(assets,'dependency-lock')
    staged='/opt/sandbox-graph/releases/cube-'+bundle['sha256']
    env={'ONE_CLICK_DEPLOY_ROLE':'control' if 'control' in roles else 'compute',
        'CUBE_SANDBOX_INSTALL_ROOT':root,'ONE_CLICK_ENV_FILE':config_path,'ONE_CLICK_PACKAGE_TAR':package['path']}
    # Config is rendered/validated before this step. The official installer sources it;
    # only a generated literal-assignment file may be supplied by the recipe executor.
    return (RecipeStep('verify:package','verify',(),asset=package),
        RecipeStep('verify:dependency-lock','dependency-lock',(),asset=dependencies),
        file_step(assets,'rendered-env',config_path,mode=0o600),archive_step(assets,'oneclick',staged),
        command('cube:install','bash',staged+'/install.sh','--mode='+('upgrade' if upgrade else 'install'),'--yes',
            environment=env,network='none',timeout_s=3600))


def control_recipe(*,assets,state,root,config_paths,proxy_compose_path,upgrade=False,observed_steps=None,proxy_image=None):
    """Explicit control-only deployment using pinned native units and Compose.

    Dependency services and their endpoints are declared configuration assets;
    this path installs no cubelet, VM runtime, host networking or guest pools.
    """
    if state=='partial' and observed_steps is not None:
        from .common import missing_steps
        recipe=control_recipe(assets=assets,state='absent',root=root,
            config_paths=config_paths,proxy_compose_path=proxy_compose_path,proxy_image=proxy_image)
        units={'CubeOps':'cubeops','CubeMaster':'cubemaster','CubeAPI':'cube-api',
            'CubeTemplateCenter':'cube-templatecenter'}
        dependencies={}
        for component,suffix in units.items():
            unit='cube-sandbox-'+suffix+'.service'
            dependencies['start:'+unit]=('file:'+component,'file:config:'+component,'file:'+unit)
        dependencies['validate:cubeproxy']=('file:cubeproxy.compose',)
        proxy=['validate:cubeproxy','file:cube-sandbox-cube-proxy.service']
        if 'image:cubeproxy' in assets:proxy.append('image:cubeproxy')
        dependencies['start:cube-sandbox-cube-proxy.service']=tuple(proxy)
        return missing_steps(recipe,observed_steps,dependencies=dependencies)
    if installation_state(state,upgrade):return ()
    absolute(root);absolute(proxy_compose_path)
    names={'CubeOps':'cubeops','CubeMaster':'cubemaster','CubeAPI':'cube-api','CubeTemplateCenter':'templatecenter'}
    if set(config_paths)!=set(names):raise ValueError('all native control configuration paths required')
    from .probe import UNITS
    steps=[RecipeStep('verify:dependency-lock','dependency-lock',(),asset=require_asset(assets,'dependency-lock'))]
    for component,binary in names.items():
        steps.extend([file_step(assets,component,root+'/'+component+'/bin/'+binary),
            file_step(assets,'config:'+component,config_paths[component],mode=0o600)])
    if 'image:cubeproxy' in assets:
        from .common import image_load_step
        steps.append(image_load_step(assets,'cubeproxy',proxy_image))
    steps.append(file_step(assets,'cubeproxy.compose',proxy_compose_path,mode=0o600))
    steps.append(RecipeStep('validate:cubeproxy','cube-compose',(),target=proxy_compose_path,environment={'expected_image':proxy_image}))
    for unit in UNITS['cubesandbox']['control']:
        steps.append(file_step(assets,unit,'/etc/systemd/system/'+unit,mode=0o644))
    steps.append(command('systemd:reload','systemctl','daemon-reload'))
    steps.extend(command('start:'+unit,'systemctl','start',unit) for unit in UNITS['cubesandbox']['control'])
    return tuple(steps)
