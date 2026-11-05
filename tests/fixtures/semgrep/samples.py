# Positive samples for the bundled Semgrep rules (test fixture; never executed).
import hashlib
import os
import pickle
import subprocess

import requests
import yaml


def bad(user_input, blob, url):
    eval(user_input)
    subprocess.run(user_input, shell=True)
    os.system(user_input)
    pickle.loads(blob)
    yaml.load(blob)
    hashlib.md5(blob)
    requests.get(url, verify=False)
