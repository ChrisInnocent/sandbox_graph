"""Source-pinned product recipes; installation evidence is produced only by execution."""
from importlib import import_module


def product_adapter(product):
    if product not in {'cubesandbox','kuasar','agentenv'}: raise ValueError('unknown product')
    return import_module(__name__+'.'+product)
