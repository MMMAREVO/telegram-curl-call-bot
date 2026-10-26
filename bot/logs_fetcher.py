import shlex

import asyncssh

from .config import LogSource, KUBECTL_BIN


async def fetch_call_log(source: LogSource, sid: str, since_seconds: int) -> str:
    """Достаёт строки лога, относящиеся к звонку с данным sid, через SSH + kubectl logs."""
    sudo_prefix = "sudo -S " if source.sudo_password else "sudo "
    cmd = (
        f"{sudo_prefix}{KUBECTL_BIN} logs -n {source.namespace} {source.pod} "
        f"--since={since_seconds}s | grep -F {shlex.quote(sid)}"
    )

    connect_kwargs: dict = {"username": source.user, "known_hosts": None}
    if source.key_path:
        connect_kwargs["client_keys"] = [source.key_path]
    if source.password:
        connect_kwargs["password"] = source.password

    stdin_data = f"{source.sudo_password}\n" if source.sudo_password else None

    async with asyncssh.connect(source.host, port=source.port, **connect_kwargs) as conn:
        result = await conn.run(cmd, check=False, input=stdin_data)
        output = (result.stdout or "").strip()
        if not output:
            if result.exit_status not in (0, 1):  # 1 = grep ничего не нашёл, это не ошибка
                raise RuntimeError(result.stderr or f"kubectl logs завершился с кодом {result.exit_status}")
            return "(строки лога с этим sid не найдены)"
        return output
