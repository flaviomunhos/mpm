"""Diagnóstico de rede entre RX e TX: latência e vazão com 1 e com N conexões.

Não toca no disco: o TX envia bytes aleatórios. Serve para separar "a rede é o limite"
de "o limite é o disco, o antivírus ou o próprio MPM", e para mostrar quanto a cópia
em paralelo ajuda neste caminho.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable
from typing import Any

from mpm.core.fmt import fmt_bytes
from mpm.net.rx import RxClient

MiB = 1024 * 1024


def _download(client: RxClient, size: int) -> int:
    received = 0

    def sink(block: bytes) -> None:
        nonlocal received
        received += len(block)

    client.stream({"op": "bench", "size": size}, sink)
    return received


def run_netcheck(
    client: RxClient,
    workers: list[RxClient],
    *,
    pings: int = 30,
    size: int = 64 * MiB,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    log("\n[Netcheck] Medindo a rede entre RX e TX (nenhum arquivo é lido ou gravado)\n")

    rtts: list[float] = []
    for i in range(pings):
        t0 = time.perf_counter()
        client.request({"op": "ping", "t": i})
        rtts.append((time.perf_counter() - t0) * 1000)
    rtt_avg = statistics.fmean(rtts)
    rtt_med = statistics.median(rtts)
    log(f"  Latência (ida e volta): média {rtt_avg:.1f} ms, mediana {rtt_med:.1f} ms, "
        f"mín {min(rtts):.1f} ms, máx {max(rtts):.1f} ms")
    per_conn_small = 1000 / rtt_avg if rtt_avg > 0 else float("inf")
    log(f"  Arquivos pequenos, 1 conexão: no máximo ~{per_conn_small:.0f} arquivos/s "
        "(uma ida e volta por arquivo)")

    t0 = time.perf_counter()
    got = _download(client, size)
    single = got / max(time.perf_counter() - t0, 1e-9)
    log(f"  Vazão com 1 conexão:  {fmt_bytes(single)}/s ({single * 8 / 1e6:.0f} Mbit/s)")

    result: dict[str, Any] = {
        "rtt_avg_ms": round(rtt_avg, 2), "rtt_median_ms": round(rtt_med, 2),
        "rtt_min_ms": round(min(rtts), 2), "rtt_max_ms": round(max(rtts), 2),
        "single_bytes_per_s": round(single),
        "parallel_connections": 0, "parallel_bytes_per_s": None,
    }

    if len(workers) >= 2:
        each = max(MiB, size // len(workers))
        totals = [0] * len(workers)

        def run(i: int) -> None:
            totals[i] = _download(workers[i], each)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(len(workers))]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        parallel = sum(totals) / max(time.perf_counter() - t0, 1e-9)
        gain = parallel / single if single else 0
        log(f"  Vazão com {len(workers)} conexões: {fmt_bytes(parallel)}/s "
            f"({parallel * 8 / 1e6:.0f} Mbit/s), {gain:.1f}x a de 1 conexão")
        result.update(parallel_connections=len(workers), parallel_bytes_per_s=round(parallel),
                      parallel_gain=round(gain, 2))
        best = max(single, parallel)
    else:
        best = single

    log("")
    if best < 12 * MiB:
        log(f"  Leitura: a rede entrega no máximo ~{fmt_bytes(best)}/s neste caminho. "
            "Se os dois estão em Wi-Fi, teste com cabo; se já estão em cabo, "
            "verifique a velocidade das portas (um trecho de 100 Mbit/s limita a ~11 MB/s).")
    elif result.get("parallel_gain", 1) >= 1.3:
        log("  Leitura: mais conexões aumentam a vazão; a cópia em paralelo vai ajudar.")
    else:
        log("  Leitura: a rede está boa e 1 conexão já a ocupa; se a cópia real ficar bem abaixo "
            "disso, o limite está no disco ou no antivírus de uma das máquinas.")
    return result
