from pathlib import Path

import httpx


async def download_recording(url: str, dest: Path) -> int:
    """Скачивает аудиозапись звонка по ссылке из rec_file и сохраняет в dest. Возвращает размер в байтах."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return len(resp.content)
