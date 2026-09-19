from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from youtube_bot.config import Settings
from youtube_bot.youtube.client import (
    YouTubeClient,
    YouTubeLiveChatUnavailableError,
    YouTubeLiveEndedError,
    YouTubeQuotaExceededError,
)
from youtube_bot.youtube.live import LiveChatStopReason
from youtube_bot.youtube.quota import QuotaGuardTriggered

logger = logging.getLogger(__name__)

_WEEKDAY_BY_NAME = {
    "mon": 0,
    "monday": 0,
    "seg": 0,
    "segunda": 0,
    "tue": 1,
    "tuesday": 1,
    "ter": 1,
    "terca": 1,
    "quarta": 2,
    "wed": 2,
    "wednesday": 2,
    "qua": 2,
    "thu": 3,
    "thursday": 3,
    "qui": 3,
    "quinta": 3,
    "fri": 4,
    "friday": 4,
    "sex": 4,
    "sexta": 4,
    "sat": 5,
    "saturday": 5,
    "sab": 5,
    "sabado": 5,
    "sun": 6,
    "sunday": 6,
    "dom": 6,
    "domingo": 6,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str, variable_name: str) -> time:
    try:
        return time.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"{variable_name} deve estar no formato HH:MM; recebeu {value!r}."
        ) from exc


def _parse_weekdays(values: tuple[str, ...]) -> frozenset[int]:
    weekdays: set[int] = set()
    invalid: list[str] = []
    for value in values:
        normalized = value.strip().lower().replace("-feira", "")
        weekday = _WEEKDAY_BY_NAME.get(normalized)
        if weekday is None:
            invalid.append(value)
        else:
            weekdays.add(weekday)

    if invalid:
        raise ValueError(
            "YOUTUBE_LIVE_SCHEDULE_DAYS contem dias invalidos: "
            + ", ".join(invalid)
        )
    if not weekdays:
        raise ValueError("YOUTUBE_LIVE_SCHEDULE_DAYS nao pode ficar vazio.")
    return frozenset(weekdays)


@dataclass(frozen=True)
class LiveDiscoverySchedule:
    """Regra de calendario para a descoberta de uma live publica."""

    zone: ZoneInfo
    weekdays: frozenset[int]
    start_time: time
    end_time: time
    poll_interval: timedelta
    resume_grace: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> "LiveDiscoverySchedule":
        try:
            zone = ZoneInfo(settings.youtube_live_schedule_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                "Fuso horario invalido em YOUTUBE_LIVE_SCHEDULE_TIMEZONE: "
                f"{settings.youtube_live_schedule_timezone!r}."
            ) from exc

        start_time = _parse_time(
            settings.youtube_live_schedule_start,
            "YOUTUBE_LIVE_SCHEDULE_START",
        )
        end_time = _parse_time(
            settings.youtube_live_schedule_end,
            "YOUTUBE_LIVE_SCHEDULE_END",
        )
        if end_time <= start_time:
            raise ValueError(
                "YOUTUBE_LIVE_SCHEDULE_END deve ser posterior ao horario inicial "
                "e estar no mesmo dia."
            )
        if settings.youtube_live_schedule_poll_minutes < 5:
            raise ValueError(
                "YOUTUBE_LIVE_SCHEDULE_POLL_MINUTES deve ser no minimo 5 para "
                "respeitar o limite padrao diario de buscas da API do YouTube."
            )
        if settings.youtube_live_resume_grace_minutes <= 0:
            raise ValueError(
                "YOUTUBE_LIVE_RESUME_GRACE_MINUTES deve ser maior que zero."
            )

        return cls(
            zone=zone,
            weekdays=_parse_weekdays(settings.youtube_live_schedule_days),
            start_time=start_time,
            end_time=end_time,
            poll_interval=timedelta(
                minutes=settings.youtube_live_schedule_poll_minutes
            ),
            resume_grace=timedelta(
                minutes=settings.youtube_live_resume_grace_minutes
            ),
        )

    def as_local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("O relogio da agenda deve fornecer um datetime com fuso.")
        return value.astimezone(self.zone)

    def is_discovery_window(self, value: datetime) -> bool:
        local = self.as_local(value)
        return (
            local.weekday() in self.weekdays
            and self.start_time <= local.timetz().replace(tzinfo=None) < self.end_time
        )

    def window_end(self, value: datetime) -> datetime:
        local = self.as_local(value)
        return datetime.combine(local.date(), self.end_time, tzinfo=self.zone)

    def next_window_start(self, value: datetime) -> datetime:
        local = self.as_local(value)
        for days_ahead in range(8):
            candidate_date = local.date() + timedelta(days=days_ahead)
            if candidate_date.weekday() not in self.weekdays:
                continue
            candidate = datetime.combine(
                candidate_date,
                self.start_time,
                tzinfo=self.zone,
            )
            if candidate > local:
                return candidate
        raise RuntimeError("Nao foi possivel calcular a proxima janela de live.")

    def next_discovery_check(self, value: datetime) -> datetime:
        local = self.as_local(value)
        return min(local + self.poll_interval, self.window_end(local))


StartLiveChat = Callable[[str], Awaitable[asyncio.Task[LiveChatStopReason]]]


class ScheduledLiveMonitor:
    """Descobre e supervisiona uma unica live dentro da agenda configurada.

    A URL encontrada fica em memoria em ``detected_live_url``. A agenda deixa
    de fazer ``search.list`` quando uma sessao de chat e iniciada e so volta a
    consultar a API durante os 30 minutos de standby apos o chat encerrar.
    """

    def __init__(
        self,
        *,
        youtube_client: YouTubeClient,
        schedule: LiveDiscoverySchedule,
        start_live_chat: StartLiveChat,
        channel_id: str = "",
        channel_handle: str = "",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.youtube_client = youtube_client
        self.schedule = schedule
        self.start_live_chat = start_live_chat
        self.channel_id = channel_id.strip()
        self.channel_handle = channel_handle.strip()
        self.clock = clock

        if not self.channel_id and not self.channel_handle:
            raise ValueError("Informe YOUTUBE_CHANNEL_ID ou YOUTUBE_CHANNEL_HANDLE.")

        self.detected_video_id: str | None = None
        self.detected_live_url: str | None = None
        self._active_task: asyncio.Task[LiveChatStopReason] | None = None
        self._standby_until: datetime | None = None
        self._finished_on: date | None = None
        self._standby_cutoff = time(21, 0)
        self.connected_at: datetime | None = None
        self.message_count = 0
        self.live_chat_id: str | None = None
        self.live_title: str | None = None
        self._manual_check = False
        self._discovery_lock = asyncio.Lock()

    async def run(self) -> None:
        logger.info(
            "Agenda de descoberta de lives ativa: fuso=%s, inicio=%s, fim=%s, "
            "intervalo=%sm.",
            self.schedule.zone.key,
            self.schedule.start_time.strftime("%H:%M"),
            self.schedule.end_time.strftime("%H:%M"),
            int(self.schedule.poll_interval.total_seconds() // 60),
        )
        try:
            while True:
                now = self._now()

                if self._active_task is not None:
                    await self._wait_for_active_live()
                    continue

                if self._standby_until is not None:
                    await self._run_standby(now)
                    continue

                if self._is_finished_today(now) or not self.schedule.is_discovery_window(now):
                    logger.info(
                        "Fora da janela de descoberta (%s-%s %s). Próxima checagem agendada: %s.",
                        self.schedule.start_time.strftime("%H:%M"),
                        self.schedule.end_time.strftime("%H:%M"),
                        self.schedule.zone.key,
                        self.schedule.next_window_start(now).strftime("%Y-%m-%d %H:%M"),
                    )
                    await self._sleep_until(self.schedule.next_window_start(now))
                    continue

                started = await self._discover_and_start_live()
                if not started and not self._is_finished_today(self._now()):
                    await self._sleep_until(
                        self.schedule.next_discovery_check(self._now())
                    )
        except asyncio.CancelledError:
            logger.info("Agenda de descoberta de lives encerrada.")
            raise

    async def stop(self) -> None:
        """Cancela com seguranca a sessao de chat controlada por esta agenda."""
        if self._active_task is None or self._active_task.done():
            return
        self._active_task.cancel()
        await asyncio.gather(self._active_task, return_exceptions=True)

    async def force_check(self) -> bool:
        """Executa uma descoberta única sem aplicar a janela da agenda."""
        if self._active_task is not None and not self._active_task.done():
            return True
        self._manual_check = True
        try:
            return await self._discover_and_start_live()
        finally:
            self._manual_check = False

    async def disconnect(self) -> bool:
        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._active_task = None
        self.connected_at = None
        self.live_chat_id = None
        return True

    def status(self) -> dict[str, object]:
        connected = self._active_task is not None and not self._active_task.done()
        now = self._now()
        return {
            "connected": connected,
            "video_id": self.detected_video_id if connected else None,
            "title": self.live_title if connected else None,
            "live_chat_id": self.live_chat_id if connected else None,
            "connected_since": self.connected_at.isoformat() if connected and self.connected_at else None,
            "messages_processed": self.message_count if connected else 0,
            "next_scheduled_check": (
                None if self.schedule.is_discovery_window(now)
                else self.schedule.next_window_start(now).strftime("%Y-%m-%d %H:%M %Z")
            ),
            "outside_window": not self.schedule.is_discovery_window(now),
        }

    def _now(self) -> datetime:
        return self.schedule.as_local(self.clock())

    def _is_finished_today(self, now: datetime) -> bool:
        return self._finished_on == now.date()

    async def _sleep_until(self, target: datetime) -> None:
        seconds = max(0.0, (target - self._now()).total_seconds())
        await asyncio.sleep(seconds)

    async def _resolve_channel_id(self) -> str | None:
        if self.channel_id:
            return self.channel_id

        channel_id = await self.youtube_client.resolve_channel_id(self.channel_handle)
        if channel_id:
            self.channel_id = channel_id
            return channel_id

        logger.error(
            "Canal @%s nao encontrado. A descoberta sera desativada ate a proxima "
            "janela agendada.",
            self.channel_handle.lstrip("@"),
        )
        self._finish_today("canal nao encontrado")
        return None

    async def _discover_and_start_live(self) -> bool:
        async with self._discovery_lock:
            return await self._discover_and_start_live_unlocked()

    async def _discover_and_start_live_unlocked(self) -> bool:
        try:
            channel_id = await self._resolve_channel_id()
        except (YouTubeQuotaExceededError, QuotaGuardTriggered):
            if self._manual_check:
                raise
            logger.warning(
                "Quota do YouTube esgotada ao resolver o canal. A descoberta sera "
                "desativada ate a proxima janela agendada."
            )
            self._finish_today("quota excedida")
            return False
        except Exception as exc:
            logger.warning(
                "API do YouTube indisponivel ao resolver o canal: %s. "
                "Nova tentativa no proximo intervalo.",
                exc,
            )
            return False

        if not channel_id:
            return False

        try:
            video_id = await self.youtube_client.find_active_live_video_id(channel_id)
        except (YouTubeQuotaExceededError, QuotaGuardTriggered):
            if self._manual_check:
                raise
            logger.warning(
                "Quota do YouTube esgotada durante a busca de live. A descoberta "
                "sera desativada ate a proxima janela agendada."
            )
            self._finish_today("quota excedida")
            return False
        except Exception as exc:
            logger.warning(
                "API do YouTube indisponivel ao buscar live do canal %s: %s. "
                "Nova tentativa no proximo intervalo.",
                channel_id,
                exc,
            )
            return False

        if not video_id:
            logger.info("Nenhuma live ativa encontrada no canal %s.", channel_id)
            return False

        self.detected_video_id = video_id
        self.live_title = self.youtube_client.last_live_title
        self.detected_live_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info(
            "Live detectada automaticamente: %s. Usando o ID em memoria; "
            "nenhuma alteracao no .env e necessaria.",
            self.detected_live_url,
        )

        try:
            self._active_task = await self.start_live_chat(video_id)
        except (YouTubeQuotaExceededError, QuotaGuardTriggered):
            if self._manual_check:
                raise
            logger.warning(
                "Quota do YouTube esgotada ao conectar na live %s. A descoberta "
                "sera desativada ate a proxima janela agendada.",
                video_id,
            )
            self._finish_today("quota excedida")
            return False

        except YouTubeLiveChatUnavailableError as exc:
            logger.error(
                "Live %s foi encontrada, mas o chat nao esta disponivel (%s). "
                "A descoberta sera encerrada para hoje.",
                video_id,
                exc,
            )
            self._finish_today("live encontrada sem chat ativo")
            return False
        except YouTubeLiveEndedError:
            logger.info(
                "A live %s terminou antes da conexao. A busca continuara na janela.",
                video_id,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Falha temporaria ao conectar na live %s: %s. Nova tentativa no "
                "proximo intervalo.",
                video_id,
                exc,
            )
            return False

        self.connected_at = self._now()
        self.live_chat_id = getattr(self._active_task, "live_chat_id", None)

        logger.info(
            "Chat da live %s iniciado; novas buscas serao interrompidas enquanto "
            "a sessao estiver ativa.",
            video_id,
        )
        return True

    async def _wait_for_active_live(self) -> None:
        task = self._active_task
        if task is None:
            return

        try:
            reason = await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("A tarefa do chat da live terminou com erro inesperado.")
            reason = LiveChatStopReason.CONNECTION_ERROR
        finally:
            self._active_task = None

        if reason is LiveChatStopReason.QUOTA_EXCEEDED:
            logger.warning(
                "Chat da live parou por quota excedida. Nenhuma nova busca sera feita "
                "ate a proxima janela agendada."
            )
            self._finish_today("quota excedida durante o chat")
            return

        if self._now().timetz().replace(tzinfo=None) >= self._standby_cutoff:
            self._finish_today("live encerrada apos 21:00")
            return

        self._standby_until = self._now() + self.schedule.resume_grace
        logger.warning(
            "Chat da live %s encerrou (%s). Entrando em standby ate %s para verificar "
            "se a transmissao volta.",
            self.detected_video_id,
            reason.value,
            self._standby_until.strftime("%Y-%m-%d %H:%M %Z"),
        )

    async def _run_standby(self, now: datetime) -> None:
        standby_until = self._standby_until
        if standby_until is None:
            return

        if now >= standby_until or now.timetz().replace(tzinfo=None) >= self._standby_cutoff:
            logger.info(
                "A live nao voltou dentro do standby de %s minutos. Descoberta "
                "desativada ate a proxima janela agendada.",
                int(self.schedule.resume_grace.total_seconds() // 60),
            )
            self._standby_until = None
            self._finish_today("standby expirado")
            return

        started = await self._discover_and_start_live()
        if started or self._is_finished_today(self._now()):
            self._standby_until = None
            return

        await self._sleep_until(
            min(
                self._now() + self.schedule.poll_interval,
                standby_until,
                datetime.combine(now.date(), self._standby_cutoff, tzinfo=self.schedule.zone),
            )
        )

    def _finish_today(self, reason: str) -> None:
        today = self._now().date()
        if self._finished_on == today:
            return
        self._finished_on = today
        logger.info(
            "Descoberta de lives encerrada para %s (%s).",
            today.isoformat(),
            reason,
        )
