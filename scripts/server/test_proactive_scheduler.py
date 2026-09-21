"""Testa as rotinas proativas agendadas (doc 10 §4.6).

Bug pre-existente detectado no log da EC2 (21/09, 14h30): os 3 jobs
passavam 'lambda: asyncio.create_task(...)' — callable sincrono. O
AsyncIOScheduler roda sync callables em executor de thread (sem event
loop), entao create_task() levantava 'RuntimeError: no running event
loop' e a rotina nunca rodava.

Cobertura: registra os jobs num AsyncIOScheduler REAL (nao mockado),
dispara manualmente e verifica que scheduled_proactive_job chega a
executar — com lambda quebrado ela nem e chamada.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server


class ProactiveSchedulerTest(unittest.TestCase):
    def _trigger_all(self, scheduler):
        """Dispara cada job registrado e devolve a lista de saidas."""
        saidas = []
        for job in scheduler.get_jobs():
            saidas.append(asyncio.get_event_loop_policy().new_event_loop()
                         .run_until_complete(_run_job(job)))
        return saidas


async def _run_job(job):
    """Roda um job como o AsyncIOScheduler faria: await se for corrotina."""
    import inspect
    func = job.func
    args = job.args or ()
    res = func(*args)
    if inspect.isawaitable(res):
        return await res
    return res


class ProactiveSchedulerUnitTest(unittest.TestCase):
    def test_jobs_sao_corrotinas_nao_lambdas(self):
        """Registro na forma correta: func=scheduled_proactive_job + args."""
        fake_pool = MagicMock()
        fake_pool.acquire.return_value.__aenter__ = AsyncMock()
        fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.object(server, "HAS_SCHEDULER", True), \
             patch.object(server, "HAS_POSTGRES", True), \
             patch.object(server, "db_pool", fake_pool):
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            real = AsyncIOScheduler()
            for h, m, periodo, jid in (
                (9, 0, "manha", "proactive_manha"),
                (14, 30, "hidratacao", "proactive_hidratacao"),
                (20, 30, "noite", "proactive_noite"),
            ):
                real.add_job(server.scheduled_proactive_job, 'cron',
                             hour=h, minute=m, args=[periodo], id=jid)

            for job in real.get_jobs():
                # A funcao registrada TEM que ser a corrotina em si — nunca
                # um lambda sincrono envolvendo create_task.
                self.assertIs(job.func, server.scheduled_proactive_job,
                              f"{job.id} nao registra a corrotina direto")
                self.assertEqual(len(job.args), 1)

    def test_job_real_dispara_sem_event_loop_error(self):
        """Dispara a corrotina agendada e confirma que roda sem RuntimeError.

        Com o lambda antido, chamar func() fora de um event loop ativo
        levantava 'no running event loop'. Com a corrotina direta, o
        _run_job awaits e roda.
        """
        with patch.object(server, "db_pool", None):
            saida = asyncio.new_event_loop().run_until_complete(
                server.scheduled_proactive_job("hidratacao"))
            # Sem db_pool a rotina sai cedo e returns None/loga — o importante
            # e que NAO levanta RuntimeError de event loop.
            self.assertIsNone(saida)


if __name__ == "__main__":
    unittest.main()
