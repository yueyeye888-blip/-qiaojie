"""Read-only enumeration; unrelated USB details are never retained."""
import plistlib
import subprocess


def count_matches(tree,vendor,product):
    matches=0
    if isinstance(tree,dict):
        if tree.get('idVendor')==vendor and tree.get('idProduct')==product:
            matches+=1
        for value in tree.values():
            matches+=count_matches(value,vendor,product)
    elif isinstance(tree,list):
        for item in tree:
            matches+=count_matches(item,vendor,product)
    return matches


def inspect_usb(vendor,product):
    try:
        result=subprocess.run(['/usr/sbin/ioreg','-a','-l','-p','IOUSB'],capture_output=True,timeout=10,check=True)
        return count_matches(plistlib.loads(result.stdout),int(vendor,16),int(product,16))
    except Exception:
        return None
