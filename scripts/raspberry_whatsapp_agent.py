#!/usr/bin/env python3
"""Agente opcional para Raspberry Pi.

Fluxo:
1. Consulta o endpoint /api/nursery-feed-digest do AquaSmart
2. Monta a mensagem consolidada do dia
3. Envia ao grupo do WhatsApp Web usando Playwright

Variáveis de ambiente sugeridas:
- AQUASMART_BASE_URL=https://aquasmart-1.onrender.com/
- NURSERY_DIGEST_TOKEN=abc123
- WHATSAPP_GROUP_NAME=Fazenda Aqua Smart
- WHATSAPP_PROFILE_DIR=/home/pi/.aqua_whatsapp_profile
- DRY_RUN=1  # imprime no terminal sem enviar
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = os.getenv('AQUASMART_BASE_URL', 'http://127.0.0.1:5000').rstrip('/')
TOKEN = os.getenv('NURSERY_DIGEST_TOKEN', '').strip()
GROUP_NAME = os.getenv('WHATSAPP_GROUP_NAME', 'aqua smart').strip()
PROFILE_DIR = Path(os.getenv('WHATSAPP_PROFILE_DIR', str(Path.home() / '.aqua_whatsapp_profile')))
DRY_RUN = os.getenv('DRY_RUN', '0') == '1'


def fetch_digest() -> dict:
    url = f"{BASE_URL}/api/nursery-feed-digest?{urllib.parse.urlencode({'token': TOKEN})}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode('utf-8'))


def send_via_whatsapp(message: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError('Playwright não está instalado na Raspberry. Instale com: pip install playwright && playwright install chromium') from exc

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto('https://web.whatsapp.com', wait_until='domcontentloaded')
        page.wait_for_timeout(5000)

        search_selectors = [
            'div[contenteditable="true"][data-tab="3"]',
            'div[contenteditable="true"][data-tab="10"]',
        ]
        search_box = None
        for selector in search_selectors:
            try:
                search_box = page.locator(selector).first
                search_box.wait_for(timeout=8000)
                break
            except Exception:
                continue
        if search_box is None:
            raise RuntimeError('Não encontrei a busca do WhatsApp. Confira se a sessão está logada.')

        search_box.click()
        page.keyboard.press('Control+A')
        page.keyboard.press('Backspace')
        search_box.fill(GROUP_NAME)
        page.wait_for_timeout(2500)
        page.locator(f'text={GROUP_NAME}').first.click(timeout=10000)

        message_box = page.locator('div[contenteditable="true"][data-tab="10"]').last
        message_box.click()
        message_box.fill(message)
        page.keyboard.press('Enter')
        page.wait_for_timeout(2000)
        browser.close()


def main() -> int:
    data = fetch_digest()
    if not data.get('ok'):
        print('Falha ao buscar digest:', data, file=sys.stderr)
        return 1

    message = data.get('combined_message', '').strip()
    if not message:
        print('Nenhum berçário ativo encontrado para enviar hoje.')
        return 0

    if DRY_RUN:
        print(message)
        return 0

    send_via_whatsapp(message)
    print('Mensagem enviada com sucesso.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
