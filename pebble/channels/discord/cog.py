"""Message handling cog for the Discord channel adapter.

Handles ``on_message`` events and slash commands (``/link``, ``/unlink``,
``/ask``, ``/status``, ``/close``).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING

from pebble.core.log import get_logger

if TYPE_CHECKING:
    import discord
    from discord import app_commands
    from discord.ext import commands

    from pebble.channels.discord.bot import TurnstoneBot

log = get_logger(__name__)

_THREAD_NAME_MAX = 100
_DM_REPLY_MAX_LENGTH = 4096  # Discord's own message limit

# /link is the only flow that reads Turnstone API tokens out of user
# input; throttle aggressively so an attacker with throw-away Discord
# accounts can't online-enumerate valid tokens.
_LINK_RATE_WINDOW_S: float = 3600.0
_LINK_RATE_LIMIT: int = 5
_LINK_RATE_CAP: int = 2048


class MessageCog:
    """Cog that processes messages and registers slash commands.

    Accessed via ``bot.turnstone`` to reach the :class:`TurnstoneBot` wrapper.
    """

    def __init__(self, bot: commands.Bot) -> None:
        import discord
        from discord import app_commands
        from discord.ext import commands as _commands

        self.bot = bot
        self.ts: TurnstoneBot = bot.turnstone  # type: ignore[attr-defined]

        # Per-Discord-user sliding-window rate limit on /link, to block
        # online enumeration of Turnstone API tokens.
        self._link_buckets: OrderedDict[str, deque[float]] = OrderedDict()

        # -- Cog wiring (manual since we can't use decorators with guarded imports) --

        # We build the cog dynamically so discord.py's import is fully deferred.
        cog_self = self

        class _Cog(_commands.Cog, name="Turnstone"):  # type: ignore[call-arg]
            """Turnstone Discord integration."""

            @_commands.Cog.listener()
            async def on_message(self_cog: _Cog, message: discord.Message) -> None:  # noqa: N805
                await cog_self._on_message(message)

            @app_commands.command(name="link", description="Link your Discord account to Turnstone")
            async def link(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await interaction.response.send_modal(cog_self._link_modal_cls(cog_self))

            @app_commands.command(
                name="unlink", description="Unlink your Discord account from Turnstone"
            )
            async def unlink(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_unlink(interaction)

            @app_commands.command(name="ask", description="Start a new Turnstone workstream")
            @app_commands.describe(
                message="Your message to the assistant",
                model="Model alias (leave blank for default)",
                persona="Persona to use (leave blank for default)",
            )
            async def ask(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                message: str,
                model: str = "",
                persona: str = "",
            ) -> None:
                await cog_self._cmd_ask(interaction, message, model=model, persona=persona)

            @ask.autocomplete("model")
            async def _model_autocomplete(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                return await cog_self._autocomplete_model(interaction, current)

            @ask.autocomplete("persona")
            async def _ask_persona_autocomplete(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                return await cog_self._autocomplete_persona(interaction, current)

            @app_commands.command(
                name="orchestrate",
                description="Start an orchestrator workstream in a thread",
            )
            @app_commands.describe(
                task="The task or goal for the orchestrator",
                model="Model alias (leave blank for default)",
            )
            async def orchestrate(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                task: str,
                model: str = "",
            ) -> None:
                await cog_self._cmd_orchestrate(interaction, task, model=model)

            @orchestrate.autocomplete("model")
            async def _orchestrate_model_autocomplete(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                return await cog_self._autocomplete_model(interaction, current)

            @app_commands.command(name="status", description="Show workstream status")
            async def status(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_status(interaction)

            @app_commands.command(name="close", description="Close the current workstream")
            async def close(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_close(interaction)

            @app_commands.command(
                name="start-session",
                description="Start a Turnstone session in this channel (all messages are fed to the agent)",
            )
            @app_commands.describe(
                model="Model alias (leave blank for default)",
                persona="Persona to use (leave blank for default)",
            )
            async def start_session(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                model: str = "",
                persona: str = "",
            ) -> None:
                await cog_self._cmd_start_session(interaction, model=model, persona=persona)

            @start_session.autocomplete("model")
            async def _session_model_autocomplete(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                return await cog_self._autocomplete_model(interaction, current)

            @start_session.autocomplete("persona")
            async def _session_persona_autocomplete(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                return await cog_self._autocomplete_persona(interaction, current)

            @app_commands.command(
                name="stop-session",
                description="Stop the Turnstone session in this channel",
            )
            async def stop_session(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_stop_session(interaction)

            @app_commands.command(
                name="global-link",
                description="Link an API token for the entire server (any member can use the bot)",
            )
            async def global_link(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await interaction.response.send_modal(cog_self._global_link_modal_cls(cog_self))

            @app_commands.command(
                name="list-persona",
                description="List available personas for workstream creation",
            )
            async def list_persona(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_list_persona(interaction)

            @app_commands.command(
                name="set-persona",
                description="Set THIS server's default persona for new chats",
            )
            @app_commands.describe(
                persona="Persona name to use by default in this server",
            )
            async def set_persona(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                persona: str,
            ) -> None:
                await cog_self._cmd_set_persona(interaction, persona)

            @set_persona.autocomplete("persona")
            async def _set_persona_autocomplete(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                return await cog_self._autocomplete_persona(
                    interaction, current, kind="interactive"
                )

            @app_commands.command(
                name="help",
                description="Show available Turnstone commands",
            )
            async def help_cmd(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_help(interaction)

        self._cog = _Cog()

        # -- Modal for /link (avoids token appearing in slash command audit logs) --
        class _LinkTokenModal(discord.ui.Modal, title="Link Account"):  # type: ignore[call-arg]
            token: discord.ui.TextInput[_LinkTokenModal] = discord.ui.TextInput(
                label="API Token",
                style=discord.TextStyle.short,
                placeholder="Paste your ts_... API token",
                required=True,
            )

            def __init__(modal_self, cog: MessageCog) -> None:  # noqa: N805
                super().__init__()
                modal_self._cog = cog

            async def on_submit(modal_self, interaction: discord.Interaction) -> None:  # noqa: N805
                await modal_self._cog._cmd_link(interaction, str(modal_self.token))

        self._link_modal_cls = _LinkTokenModal

        # -- Modal for /global-link (guild-level token, same API key pattern) --
        class _GlobalLinkTokenModal(discord.ui.Modal, title="Global Link"):  # type: ignore[call-arg]
            token: discord.ui.TextInput[_GlobalLinkTokenModal] = discord.ui.TextInput(
                label="API Token",
                style=discord.TextStyle.short,
                placeholder="Paste your ts_... API token",
                required=True,
            )

            def __init__(modal_self, cog: MessageCog) -> None:  # noqa: N805
                super().__init__()
                modal_self._cog = cog

            async def on_submit(modal_self, interaction: discord.Interaction) -> None:  # noqa: N805
                await modal_self._cog._cmd_global_link(interaction, str(modal_self.token))

        self._global_link_modal_cls = _GlobalLinkTokenModal

    # -- on_message ----------------------------------------------------------

    async def _on_message(self, message: discord.Message) -> None:
        """Route incoming messages to existing workstream threads or sessions."""
        import discord

        # Ignore self and other bots.
        if message.author == self.bot.user or message.author.bot:
            return

        # DM handling — route replies to tracked notifications.
        if message.guild is None:
            await self._handle_dm(message)
            return

        channel = message.channel

        # --- Message in a Discord Thread ---
        if isinstance(channel, discord.Thread):
            parent_id = channel.parent_id or 0
            if not self.ts._is_allowed_channel(parent_id):
                return

            # Check if this thread has an existing route (TTL-cached).
            existing_ws_id = await self.ts.router.lookup_ws_id("discord", str(channel.id))
            if existing_ws_id is None:
                return

            # Who may inject into this thread?  In a global-linked server every
            # member participates communally (like a channel session) until
            # /close; otherwise only the thread's own invoker may.  Resolving
            # the acting user (member link, else guild link) also survives a
            # gateway restart that clears the in-memory invoker map — which
            # would otherwise drop even the invoker's follow-ups (the map miss
            # falls back to channel.owner_id, i.e. the bot itself).
            guild_id = message.guild.id if message.guild else None
            acting_user_id = await self._resolve_acting_user(guild_id, message.author.id)
            communal = await self._check_guild_access(guild_id)
            if not communal:
                # Not global-linked: only the individually-linked invoker.
                effective_owner_id = self.ts.get_thread_invoker(channel.id)
                if effective_owner_id is None:
                    effective_owner_id = channel.owner_id
                if effective_owner_id is None or message.author.id != effective_owner_id:
                    log.debug(
                        "discord.thread_message_rejected_non_owner",
                        thread_id=channel.id,
                        author_id=message.author.id,
                        owner_id=effective_owner_id,
                    )
                    return
                if acting_user_id is None:
                    return
            elif acting_user_id is None:
                # Global-linked guild but no resolvable user (shouldn't happen).
                return

            try:
                ws_id, is_new = await self.ts.router.get_or_create_workstream(
                    "discord",
                    str(channel.id),
                    name=channel.name or "",
                    initial_message="",
                    client_type="chat",
                    acting_user_id=acting_user_id,
                )
            except (TimeoutError, RuntimeError):
                log.warning("discord.ws_reactivation_failed", thread_id=channel.id)
                return

            if is_new:
                await channel.send("*Workstream reactivated.*")

            if ws_id not in self.ts._subscribed_ws:
                await self.ts.subscribe_ws(ws_id, channel)

            attachment_ids = await self._upload_discord_attachments(ws_id, message)
            text = await self._enrich_message(message, message.content)
            if not text:
                if attachment_ids:
                    names = [a.filename for a in message.attachments if a.filename]
                    text = f"[Sent: {', '.join(names[:3])}]" if names else "[Sent attachment]"
                else:
                    return
            # In a communal (global-linked) thread multiple people speak, so
            # attribute each message the way channel-wide sessions do.
            if communal:
                text = f"[{message.author.display_name}]: {text}"
            await self.ts.router.send_message(ws_id, text, attachment_ids=attachment_ids)
            log.debug(
                "discord.message_routed",
                ws_id=ws_id,
                thread_id=channel.id,
                author=str(message.author),
            )
            return

        # --- Channel-wide session routing (anyone can participate) ---
        if channel.id in self.ts._channel_sessions:
            ws_id = self.ts._channel_sessions[channel.id][0]
            attachment_ids = await self._upload_discord_attachments(ws_id, message)
            text = await self._enrich_message(message, message.content)
            if not text and not attachment_ids:
                return
            if not text:
                names = [a.filename for a in message.attachments if a.filename]
                text = f"[Sent: {', '.join(names[:3])}]" if names else "[Sent attachment]"
            content = f"[{message.author.display_name}]: {text}"
            await self.ts.router.send_message(ws_id, content, attachment_ids=attachment_ids)
            log.debug(
                "discord.session_message_routed",
                ws_id=ws_id,
                channel_id=channel.id,
                author=str(message.author),
            )
            return

        # --- @mention in a non-thread channel ---
        if self.bot.user is not None and self.bot.user.mentioned_in(message):
            if not self.ts._is_allowed_channel(channel.id):
                return

            content = await self._enrich_message(message, message.content)
            if self.bot.user is not None:
                content = content.replace(f"<@{self.bot.user.id}>", "").strip()
                content = content.replace(f"<@!{self.bot.user.id}>", "").strip()

            if not content:
                content = "Hello"

            mention_model = await self.ts.router.get_channel_default_alias()
            if not mention_model:
                mention_model = self.ts.config.model

            mention_acting_user = await self._resolve_acting_user(
                message.guild.id if message.guild else None, message.author.id
            )
            mention_persona = await self._effective_persona(
                message.guild.id if message.guild else None, ""
            )
            mention_text = f"[{message.author.display_name}]: {content}"
            ws_id = await self.ts.start_channel_session(
                channel,
                discord_user_id=str(message.author.id),
                model=mention_model,
                persona=mention_persona,
                acting_user_id=mention_acting_user or "",
            )
            attachment_ids = await self._upload_discord_attachments(ws_id, message)
            await self.ts.router.send_message(ws_id, mention_text, attachment_ids=attachment_ids)
            log.info(
                "discord.mention_session_started",
                channel_id=channel.id,
                author=str(message.author),
            )
            return

    # -- DM reply handling ---------------------------------------------------

    async def _handle_dm(self, message: discord.Message) -> None:
        """Route DM replies to tracked notification workstreams."""
        # Only handle explicit replies to a tracked notification message.
        ref = message.reference
        if ref is None or ref.message_id is None:
            await message.channel.send(
                "*Direct messages aren't supported. "
                "Use `/ask` in a server channel or @mention me to start a conversation.*"
            )
            return

        # Atomic pop prevents TOCTOU race across await points.
        entry = self.ts._notify_ws_map.pop(ref.message_id, None)
        if entry is None:
            # NOTE: This also fires for replies to non-notification bot
            # messages in DMs (false positive).  Acceptable because DM
            # interactions are almost exclusively notification-driven.
            await message.channel.send("*This notification is no longer active.*")
            return

        ws_id, target_user_id = entry

        # Defence in depth: verify the replying user is the notification
        # recipient.  Discord enforces this (DMs are private), but a
        # server-side check prevents cross-user injection via compromised
        # accounts or API-level forgery.
        if str(message.author.id) != target_user_id:
            # Re-insert so the legitimate user can still reply.
            self.ts._notify_ws_map[ref.message_id] = entry
            log.warning(
                "discord.notification_reply_user_mismatch",
                expected=target_user_id,
                actual=str(message.author.id),
            )
            return

        # Resolve user identity — unlinked users are silently ignored.
        # Re-insert the tracking entry so the user can retry after linking.
        user_id = await self.ts.router.resolve_user("discord", str(message.author.id))
        if user_id is None:
            self.ts._notify_ws_map[ref.message_id] = entry
            return

        # Route the reply to the originating workstream.
        content = message.content[:_DM_REPLY_MAX_LENGTH]
        await self.ts.router.send_message(ws_id, content)

        # Register the DM channel for response forwarding.  The bot's
        # _on_ws_event handler will send the next turn's response here,
        # track the response for further replies, and clean up on
        # StreamEndEvent.
        self.ts._notify_reply_channels[ws_id] = (message.channel, target_user_id)

        log.info(
            "discord.notification_reply_routed",
            ws_id=ws_id,
            author=str(message.author),
        )

    # -- slash commands ------------------------------------------------------

    def _allow_link_attempt(self, discord_user_id: str) -> bool:
        """Return True when this Discord user is under the /link rate limit.

        Sliding window: up to ``_LINK_RATE_LIMIT`` attempts per
        ``_LINK_RATE_WINDOW_S`` seconds.  Each attempt — success or
        failure — consumes a slot.  The bucket map is LRU-bounded.
        """
        now = time.monotonic()
        window_start = now - _LINK_RATE_WINDOW_S
        bucket = self._link_buckets.get(discord_user_id)
        if bucket is None:
            bucket = deque()
            self._link_buckets[discord_user_id] = bucket
            while len(self._link_buckets) > _LINK_RATE_CAP:
                self._link_buckets.popitem(last=False)
        else:
            self._link_buckets.move_to_end(discord_user_id)
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= _LINK_RATE_LIMIT:
            return False
        bucket.append(now)
        return True

    async def _cmd_link(self, interaction: discord.Interaction, token: str) -> None:
        """Link a Discord user to a turnstone account via API token."""
        from pebble.core.auth import hash_token

        if not self._allow_link_attempt(str(interaction.user.id)):
            log.warning(
                "discord.link_rate_limited",
                discord_user=str(interaction.user),
            )
            await interaction.response.send_message(
                (
                    f"Too many /link attempts.  Try again later — limit is "
                    f"{_LINK_RATE_LIMIT} per hour."
                ),
                ephemeral=True,
            )
            return

        # Check if already linked.
        existing = await asyncio.to_thread(
            self.ts.storage.get_channel_user, "discord", str(interaction.user.id)
        )
        if existing:
            await interaction.response.send_message(
                "Your Discord account is already linked. Use `/unlink` first.",
                ephemeral=True,
            )
            return

        token_hash = hash_token(token)
        token_record = await asyncio.to_thread(self.ts.storage.get_api_token_by_hash, token_hash)

        if token_record is None:
            await interaction.response.send_message(
                "Invalid token. Please provide a valid Turnstone API token.",
                ephemeral=True,
            )
            return

        user_id = token_record.get("user_id", "")
        if not user_id:
            await interaction.response.send_message(
                "Token has no associated user.",
                ephemeral=True,
            )
            return

        await asyncio.to_thread(
            self.ts.storage.create_channel_user,
            "discord",
            str(interaction.user.id),
            user_id,
        )

        await interaction.response.send_message("Account linked!", ephemeral=True)
        log.info(
            "discord.user_linked",
            discord_user=str(interaction.user),
            user_id=user_id,
        )

    async def _cmd_unlink(self, interaction: discord.Interaction) -> None:
        """Remove the channel user mapping for the calling Discord user."""
        deleted = await asyncio.to_thread(
            self.ts.storage.delete_channel_user,
            "discord",
            str(interaction.user.id),
        )
        if deleted:
            await interaction.response.send_message("Account unlinked.", ephemeral=True)
            log.info("discord.user_unlinked", discord_user=str(interaction.user))
        else:
            await interaction.response.send_message(
                "No linked account found.",
                ephemeral=True,
            )

    async def _cmd_global_link(self, interaction: discord.Interaction, token: str) -> None:
        """Link an API token for the entire server (guild-level)."""
        from pebble.core.auth import hash_token

        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # Rate-limit /global-link the same way as /link.
        if not self._allow_link_attempt(str(interaction.user.id)):
            await interaction.response.send_message(
                "Too many attempts. Try again later.",
                ephemeral=True,
            )
            return

        token_hash = hash_token(token)
        token_record = await asyncio.to_thread(self.ts.storage.get_api_token_by_hash, token_hash)

        if token_record is None:
            await interaction.response.send_message(
                "Invalid token. Please provide a valid Turnstone API token.",
                ephemeral=True,
            )
            return

        user_id = token_record.get("user_id", "")
        if not user_id:
            await interaction.response.send_message(
                "Token has no associated user.",
                ephemeral=True,
            )
            return

        # Upsert guild-level mapping.
        await asyncio.to_thread(
            self.ts.storage.create_channel_user,
            "guild",
            str(guild_id),
            user_id,
        )
        await interaction.response.send_message(
            "**Global link established!** All server members can now interact with Turnstone "
            "without linking individually. Use `/global-unlink` to revoke.",
            ephemeral=True,
        )
        log.info(
            "discord.guild_linked",
            guild_id=guild_id,
            user_id=user_id,
            linked_by=str(interaction.user),
        )

    async def _upload_discord_attachments(self, ws_id: str, message: discord.Message) -> list[str]:
        """Upload Discord message attachments and return attachment IDs."""
        attachment_ids: list[str] = []
        for att in message.attachments:
            try:
                data = await att.read()
            except Exception:
                log.debug("discord.attachment_read_failed", filename=att.filename)
                continue
            filename = att.filename or "file"
            mime = att.content_type or "application/octet-stream"
            try:
                if ws_id in self.ts.router._coordinator_wss:
                    resp = await self.ts.router._console.coordinator_upload_attachment(
                        ws_id, filename, data, mime_type=mime
                    )
                else:
                    resp = await self.ts.router._console.route_upload_attachment(
                        ws_id, filename, data, mime_type=mime
                    )
            except Exception:
                log.debug("discord.attachment_upload_failed", ws_id=ws_id, filename=filename)
                continue
            aid = (
                resp.attachment_id
                if hasattr(resp, "attachment_id")
                else resp.get("attachment_id", "")
            )
            if aid:
                attachment_ids.append(aid)
        return attachment_ids

    async def _enrich_message(self, message: discord.Message, text: str) -> str:
        """Append content from replied-to and linked messages."""
        import re

        ref = message.reference
        if ref is not None and ref.message_id is not None:
            try:
                ref_msg = await message.channel.fetch_message(ref.message_id)
                prefix = f"> **{ref_msg.author.display_name}**: {ref_msg.content}"
                text = f"{text}\n\n{prefix}" if text else prefix
            except Exception:
                pass

        links = re.findall(r"https://discord\.com/channels/(\d+)/(\d+)/(\d+)", text)
        for _guild_id, ch_id, msg_id in links:
            try:
                ch = self.bot.get_channel(int(ch_id))
                if ch is None:
                    continue
                m = await ch.fetch_message(int(msg_id))
                extra = f"[From {m.author.display_name} in #{ch.name}]: {m.content}"
                text += f"\n{extra}"
            except Exception:
                continue

        text = re.sub(r"https://discord\.com/channels/\d+/\d+/\d+", "", text).strip()
        return text

    async def _check_guild_access(self, guild_id: int | None) -> bool:
        """Return True if the guild has a global link (any member can use the bot)."""
        if guild_id is None:
            return False
        entry = await asyncio.to_thread(self.ts.storage.get_channel_user, "guild", str(guild_id))
        return entry is not None

    async def _resolve_acting_user(self, guild_id: int | None, author_id: int) -> str | None:
        """The turnstone user a Discord action acts as: the member's own
        ``/link`` first, else the server's ``/global-link`` user, else None.

        Threaded into workstream creation so the workstream is owned by — and
        authority-checked as — the linked user.
        """
        uid = await self.ts.router.resolve_user("discord", str(author_id))
        if uid:
            return uid
        if guild_id is not None:
            entry = await asyncio.to_thread(
                self.ts.storage.get_channel_user, "guild", str(guild_id)
            )
            if entry:
                return entry.get("user_id")
        return None

    async def _effective_persona(self, guild_id: int | None, explicit: str) -> str:
        """Explicit persona wins; otherwise the server's ``/set-persona`` choice
        (per-guild); otherwise empty (server resolves the global default)."""
        if explicit:
            return explicit
        if guild_id is None:
            return ""
        chosen = await asyncio.to_thread(self.ts.storage.get_guild_persona, str(guild_id))
        return chosen or ""

    async def _precheck_access(
        self, acting_user_id: str | None, model: str, persona_name: str
    ) -> str | None:
        """Friendly pre-flight check of a user's model/persona allow-list.

        Returns an error string to show the user, or None if allowed.  The
        authoritative gate is server-side at workstream creation; this just
        avoids creating an orphan thread before that 403.  Only explicit
        violations are reported here — an unspecified model for a restricted
        user is coerced server-side, not rejected.
        """
        if not acting_user_id:
            return None
        from pebble.core import access

        storage = self.ts.storage
        if model:
            _eff, err = await asyncio.to_thread(
                access.resolve_allowed_model, storage, acting_user_id, model, ""
            )
            if err:
                return f"You don't have access to model '{model}'."
        if persona_name:
            prow = await asyncio.to_thread(storage.get_persona_by_name, persona_name)
            if prow is not None:
                ok = await asyncio.to_thread(
                    access.persona_allowed,
                    storage,
                    acting_user_id,
                    prow.get("persona_id", ""),
                    is_kind_default=bool(prow.get("is_default")),
                )
                if not ok:
                    return f"You don't have access to persona '{persona_name}'."
        return None

    async def _cmd_ask(
        self, interaction: discord.Interaction, message: str, *, model: str = "", persona: str = ""
    ) -> None:
        """Create a new thread and workstream with an initial message."""
        import discord

        acting_user_id = await self._resolve_acting_user(interaction.guild_id, interaction.user.id)
        if acting_user_id is None:
            await interaction.response.send_message(
                "Your Discord account is not linked. Use `/link` first, "
                "or ask an admin to use `/global-link`.",
                ephemeral=True,
            )
            return

        # Per-guild persona choice fills in when none was passed explicitly.
        persona = await self._effective_persona(interaction.guild_id, persona)
        # Friendly pre-flight before we create a thread (server-side is
        # authoritative).
        access_err = await self._precheck_access(acting_user_id, model, persona)
        if access_err:
            await interaction.response.send_message(access_err, ephemeral=True)
            return

        await interaction.response.defer()

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("Cannot determine channel.", ephemeral=True)
            return

        thread_name = message[:_THREAD_NAME_MAX] if len(message) > _THREAD_NAME_MAX else message

        # Create a standalone thread in the current channel.
        if isinstance(channel, discord.TextChannel):
            thread = await channel.create_thread(
                name=thread_name,
                auto_archive_duration=self.ts.config.thread_auto_archive,  # type: ignore[arg-type]
                type=discord.ChannelType.public_thread,
            )
            # `channel.create_thread` without a starter message makes the
            # bot the thread owner, so the sec-3 gate needs to see the
            # real invoker here — otherwise `/ask` follow-ups get dropped.
            self.ts.register_thread_invoker(thread.id, interaction.user.id)
        else:
            await interaction.followup.send(
                "Cannot create a thread in this channel type.",
                ephemeral=True,
            )
            return

        # Resolve model: explicit > channel default > CLI --model > server default.
        effective_model = model
        if not effective_model:
            effective_model = await self.ts.router.get_channel_default_alias()
        if not effective_model:
            effective_model = self.ts.config.model

        ws_id, _is_new = await self.ts.router.get_or_create_workstream(
            channel_type="discord",
            channel_id=str(thread.id),
            name=thread_name,
            model=effective_model,
            persona=persona,
            initial_message="",
            client_type="chat",
            acting_user_id=acting_user_id,
        )

        await self.ts.subscribe_ws(ws_id, thread)
        await self.ts.router.send_message(ws_id, message)

        await interaction.followup.send(
            f"Workstream started in {thread.mention}",
            ephemeral=True,
        )
        log.info(
            "discord.ask_workstream_created",
            ws_id=ws_id,
            thread_id=thread.id,
            author=str(interaction.user),
        )

    async def _cmd_orchestrate(
        self, interaction: discord.Interaction, task: str, *, model: str = ""
    ) -> None:
        """Create a thread + workstream for orchestrator-style tasks."""
        import discord

        acting_user_id = await self._resolve_acting_user(interaction.guild_id, interaction.user.id)
        if acting_user_id is None:
            await interaction.response.send_message(
                "Your Discord account is not linked. Use `/link` first, "
                "or ask an admin to use `/global-link`.",
                ephemeral=True,
            )
            return

        # /orchestrate always uses the coordinator (orchestrator) persona
        # regardless of persona limits — only the model is user-selectable.
        access_err = await self._precheck_access(acting_user_id, model, "")
        if access_err:
            await interaction.response.send_message(access_err, ephemeral=True)
            return

        await interaction.response.defer()

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("Cannot determine channel.", ephemeral=True)
            return

        thread_name = task[:_THREAD_NAME_MAX] if len(task) > _THREAD_NAME_MAX else task

        if isinstance(channel, discord.TextChannel):
            thread = await channel.create_thread(
                name=thread_name,
                auto_archive_duration=self.ts.config.thread_auto_archive,
                type=discord.ChannelType.public_thread,
            )
            self.ts.register_thread_invoker(thread.id, interaction.user.id)
        else:
            await interaction.followup.send(
                "Cannot create a thread in this channel type.",
                ephemeral=True,
            )
            return

        effective_model = model
        if not effective_model:
            effective_model = await self.ts.router.get_channel_default_alias()
        if not effective_model:
            effective_model = self.ts.config.model

        ws_id, _is_new = await self.ts.router.get_or_create_workstream(
            channel_type="discord",
            channel_id=str(thread.id),
            name=thread_name,
            model=effective_model,
            initial_message="",
            client_type="chat",
            kind="coordinator",
            acting_user_id=acting_user_id,
        )

        await self.ts.subscribe_ws(ws_id, thread)
        await self.ts.router.send_message(ws_id, task)

        await interaction.followup.send(
            f"Orchestrator started in {thread.mention}",
            ephemeral=True,
        )
        log.info(
            "discord.orchestrate_workstream_created",
            ws_id=ws_id,
            thread_id=thread.id,
            author=str(interaction.user),
        )

    async def _autocomplete_persona(
        self, interaction: discord.Interaction, current: str, *, kind: str = ""
    ) -> list[app_commands.Choice[str]]:
        """Return persona suggestions for command autocomplete."""
        from discord import app_commands

        try:
            personas = await self.ts.router.list_personas(cached=True)
        except Exception:
            return []
        # Show only personas the acting user may use (empty allow-list = all;
        # the kind default is always usable).
        acting_user_id = await self._resolve_acting_user(interaction.guild_id, interaction.user.id)
        allowed = await self._allowed_persona_names(acting_user_id)
        choices: list[app_commands.Choice[str]] = []
        for p in personas:
            name = p.get("name", "")
            if not name:
                continue
            if kind:
                applies = p.get("applies_to_kinds") or []
                if kind not in applies:
                    continue
            if allowed is not None and name not in allowed:
                continue
            if current and current.lower() not in name.lower():
                continue
            choices.append(app_commands.Choice(name=name, value=name))
            if len(choices) >= 25:
                break
        return choices

    async def _allowed_persona_names(self, acting_user_id: str | None) -> set[str] | None:
        """The acting user's allowed persona NAMES, or None if unrestricted.

        The allow-list stores persona_ids, but the public persona feed exposes
        names only — so map ids→names here.  The kind default personas are
        always included so a restricted user can never lose their base persona.
        """
        if not acting_user_id:
            return None
        ids = await asyncio.to_thread(self.ts.storage.list_user_allowed_personas, acting_user_id)
        if not ids:
            return None
        rows = await asyncio.to_thread(self.ts.storage.list_personas, True)
        id_to_name = {r["persona_id"]: r["name"] for r in rows}
        names = {id_to_name[i] for i in ids if i in id_to_name}
        for r in rows:
            if r.get("is_default"):
                names.add(r["name"])
        return names

    async def _allowed_model_aliases(self, acting_user_id: str | None) -> set[str] | None:
        """The acting user's allowed model aliases, or None if unrestricted."""
        if not acting_user_id:
            return None
        aliases = await asyncio.to_thread(self.ts.storage.list_user_allowed_models, acting_user_id)
        return set(aliases) if aliases else None

    async def _autocomplete_model(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return model alias suggestions for the /ask autocomplete."""
        from discord import app_commands

        try:
            data = await self.ts.router.list_models(cached=True)
        except Exception:
            return []
        acting_user_id = await self._resolve_acting_user(interaction.guild_id, interaction.user.id)
        allowed = await self._allowed_model_aliases(acting_user_id)
        choices: list[app_commands.Choice[str]] = []
        for m in data.get("models", []):
            alias = m.get("alias", "")
            if not alias:
                continue
            if allowed is not None and alias not in allowed:
                continue
            if current and current.lower() not in alias.lower():
                continue
            choices.append(app_commands.Choice(name=alias, value=alias))
            if len(choices) >= 25:
                break
        return choices

    async def _cmd_status(self, interaction: discord.Interaction) -> None:
        """Show workstream status for the current thread."""
        import discord

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used inside a thread.",
                ephemeral=True,
            )
            return

        route = await asyncio.to_thread(
            self.ts.storage.get_channel_route, "discord", str(channel.id)
        )
        if route is None:
            await interaction.response.send_message(
                "No workstream is associated with this thread.",
                ephemeral=True,
            )
            return

        ws_id = route["ws_id"]
        node_id = route.get("node_id", "")
        created = route.get("created", "")

        embed = discord.Embed(
            title="Workstream Status",
            color=discord.Color.green(),
        )
        embed.add_field(name="Workstream ID", value=ws_id, inline=False)
        if node_id:
            embed.add_field(name="Node", value=node_id, inline=True)
        if created:
            embed.add_field(name="Created", value=created, inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _cmd_close(self, interaction: discord.Interaction) -> None:
        """Close the workstream and archive the thread."""
        import discord

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used inside a thread.",
                ephemeral=True,
            )
            return

        route = await asyncio.to_thread(
            self.ts.storage.get_channel_route, "discord", str(channel.id)
        )
        if route is None:
            await interaction.response.send_message(
                "No workstream is associated with this thread.",
                ephemeral=True,
            )
            return

        ws_id = route["ws_id"]

        # Close via server API.
        await self.ts.router.close_workstream(ws_id)

        # Delete route and unsubscribe.
        await self.ts.router.delete_route("discord", str(channel.id))
        await self.ts.unsubscribe_ws(ws_id)

        await interaction.response.send_message("Workstream closed.")
        log.info("discord.workstream_closed", ws_id=ws_id, thread_id=channel.id)

        # Archive the thread.
        try:
            await channel.edit(archived=True)
        except discord.Forbidden:
            log.warning("discord.archive_forbidden", thread_id=channel.id)

    # -- channel session commands ---------------------------------------------

    async def _cmd_start_session(
        self, interaction: discord.Interaction, *, model: str = "", persona: str = ""
    ) -> None:
        """Start a channel-wide session: all messages are forwarded to the agent."""
        import discord

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used in a text channel.",
                ephemeral=True,
            )
            return

        if channel.id in self.ts._channel_sessions:
            await interaction.response.send_message(
                "A session is already active in this channel.",
                ephemeral=True,
            )
            return

        acting_user_id = await self._resolve_acting_user(interaction.guild_id, interaction.user.id)
        persona = await self._effective_persona(interaction.guild_id, persona)
        access_err = await self._precheck_access(acting_user_id, model, persona)
        if access_err:
            await interaction.response.send_message(access_err, ephemeral=True)
            return

        effective_model = model
        if not effective_model:
            effective_model = await self.ts.router.get_channel_default_alias()
        if not effective_model:
            effective_model = self.ts.config.model

        await interaction.response.defer(ephemeral=True)

        await self.ts.start_channel_session(
            channel,
            discord_user_id=str(interaction.user.id),
            model=effective_model,
            persona=persona,
            acting_user_id=acting_user_id or "",
        )
        await interaction.followup.send(
            "**Session started!** All messages in this channel will be forwarded to "
            "Turnstone. Use `/stop-session` to end it.",
            ephemeral=True,
        )
        await channel.send(
            f"**Turnstone session started by {interaction.user.mention}** — "
            "all messages will be routed to the agent."
        )

    async def _cmd_stop_session(self, interaction: discord.Interaction) -> None:
        """Stop a channel-wide session."""

        channel = interaction.channel
        if channel is None or channel.id not in self.ts._channel_sessions:
            await interaction.response.send_message(
                "No active session in this channel.",
                ephemeral=True,
            )
            return

        await self.ts.stop_channel_session(channel.id)
        await interaction.response.send_message(
            "Session ended.",
            ephemeral=True,
        )
        await channel.send(
            f"**Turnstone session ended by {interaction.user.mention}** — "
            "messages will no longer be forwarded to the agent."
        )

    async def _cmd_help(self, interaction: discord.Interaction) -> None:
        """Show available commands."""
        import discord

        embed = discord.Embed(
            title="Turnstone Commands",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="/link",
            value="Link your Discord account to Turnstone",
            inline=False,
        )
        embed.add_field(
            name="/unlink",
            value="Unlink your Discord account",
            inline=False,
        )
        embed.add_field(
            name="/ask <message> [model]",
            value="Start a new workstream thread",
            inline=False,
        )
        embed.add_field(
            name="/start-session",
            value="Start a channel-wide session (all messages routed to agent)",
            inline=False,
        )
        embed.add_field(
            name="/stop-session",
            value="Stop the channel-wide session",
            inline=False,
        )
        embed.add_field(
            name="/status",
            value="Show current workstream status",
            inline=False,
        )
        embed.add_field(
            name="/close",
            value="Close the current workstream thread",
            inline=False,
        )
        embed.add_field(
            name="/list-persona",
            value="List available personas",
            inline=False,
        )
        embed.add_field(
            name="/set-persona <persona>",
            value="Set this server's default persona for new chats",
            inline=False,
        )
        embed.add_field(
            name="/orchestrate <task> [model] [persona]",
            value="Start an orchestrator workstream in a thread",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _cmd_list_persona(self, interaction: discord.Interaction) -> None:
        """List available personas."""

        await interaction.response.defer(ephemeral=True)
        try:
            personas = await self.ts.router.list_personas(cached=False)
        except Exception:
            await interaction.followup.send("Failed to fetch personas.", ephemeral=True)
            return

        # Scope to what the linked user may actually use (empty allow-list =
        # all; kind defaults always included).
        acting_user_id = await self._resolve_acting_user(interaction.guild_id, interaction.user.id)
        allowed = await self._allowed_persona_names(acting_user_id)
        if allowed is not None:
            personas = [p for p in personas if p.get("name") in allowed]

        if not personas:
            await interaction.followup.send("No personas available.", ephemeral=True)
            return

        lines = []
        for p in personas:
            name = p.get("name", "?")
            desc = p.get("description", "")
            if desc:
                lines.append(f"**{name}** — {desc}")
            else:
                lines.append(f"**{name}**")
        await interaction.followup.send("\n".join(lines[:10]), ephemeral=True)

    async def _cmd_set_persona(self, interaction: discord.Interaction, persona: str) -> None:
        """Set THIS server's default persona (per-guild) for new interactive chats.

        Scoped to the current Discord server and stored keyed by guild_id — so
        the same linked user can run different personas in different servers.
        Applies to /ask and @mentions when no explicit persona is given;
        /orchestrate always uses the coordinator persona.  Falls back to the
        stock interactive default when a server hasn't set one.
        """
        import discord

        channel = interaction.channel
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        acting_user_id = await self._resolve_acting_user(interaction.guild_id, interaction.user.id)
        if acting_user_id is None:
            await interaction.followup.send(
                "Your Discord account is not linked. Use `/link` first, "
                "or ask an admin to use `/global-link`.",
                ephemeral=True,
            )
            return

        prow = await asyncio.to_thread(self.ts.storage.get_persona_by_name, persona)
        if prow is None or not prow.get("enabled", True):
            await interaction.followup.send(f"Persona '{persona}' not found.", ephemeral=True)
            return
        if "interactive" not in (prow.get("applies_to_kinds") or []):
            await interaction.followup.send(
                f"Persona '{persona}' can't be used for interactive chats.", ephemeral=True
            )
            return

        # You can only pin a server persona you're actually allowed to use.
        from pebble.core import access

        ok = await asyncio.to_thread(
            access.persona_allowed,
            self.ts.storage,
            acting_user_id,
            prow.get("persona_id", ""),
            is_kind_default=bool(prow.get("is_default")),
        )
        if not ok:
            await interaction.followup.send(
                f"You don't have access to persona '{persona}'.", ephemeral=True
            )
            return

        await asyncio.to_thread(
            self.ts.storage.set_guild_persona, str(interaction.guild_id), persona
        )
        await interaction.followup.send(
            f"Server persona set to **{persona}** for new chats.", ephemeral=True
        )
        if isinstance(channel, discord.TextChannel):
            await channel.send(
                f"**Server persona set to {persona}** by {interaction.user.mention} "
                "(applies to new /ask and mentions in this server)."
            )
        log.info("discord.set_persona", persona=persona, guild_id=interaction.guild_id)
