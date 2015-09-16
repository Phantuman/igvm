import urllib2
import re

from fabric.api import run, cd, settings
from fabric.contrib import files

from managevm.utils import fail_gracefully, cmd

BASE_URL = 'http://files.innogames.de/'
PACKET_SERVER = 'packet.ig.local'
PACKET_DIR = '/www/files.innogames.de/htdocs'

run = fail_gracefully(run)

def get_images():
    try:
        image_html = urllib2.urlopen(BASE_URL, timeout=2).read()
    except urllib2.URLError:
        return None

    return re.findall(r'<a\s+href="(.+?\.tar\.gz)"', image_html)

def download_image(image):
    url = BASE_URL + image
    
    if files.exists(image):
        local_hash = run(cmd('md5sum {0}', image)).split()[0]
        
        try:
            with settings(host_string=PACKET_SERVER):
                with cd(PACKET_DIR):
                    remote_hash = run(cmd('md5sum {0}', image)).split()[0]
            if local_hash != remote_hash:
                run(cmd('rm -f {0}', image))
                run(cmd('wget -nv {0}', url))
        except:
            pass

    else:
        run(cmd('wget -nv {0}', url))


def extract_image(image, target_dir):
    run(cmd('tar xfz {0} -C {1}', image, target_dir))