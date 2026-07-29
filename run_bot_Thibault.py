import os
import asyncio

# Charger les variables depuis config.env
with open('config.env', 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            key, val = line.split('=', 1)
            os.environ[key.strip()] = val.strip()

# Lancer le bot
from padel_bot14_firebase_captcha_Thibault import run
asyncio.run(run())