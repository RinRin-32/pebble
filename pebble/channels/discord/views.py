"""Persistent interactive views for Discord approval.

These views use static ``custom_id`` values so they survive bot restarts.
Correlation information (``ws_id`` and ``correlation_id``) is stored in the
embed footer of the message the view is attached to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pebble.channels._routing import pop_cycle_entry
from pebble.core.log import get_logger

if TYPE_CHECKING:
    import discord

    from pebble.channels.discord.bot import TurnstoneBot

log = get_logger(__name__)


def _parse_footer(interaction: discord.Interaction) -> tuple[str, str, str] | None:
    """Extract ``(ws_id, correlation_id, owner_id)`` from the first embed's footer.

    Footer format is ``"{ws_id}|{correlation_id}|{owner_id}"``.  Older
    posts that pre-date the owner-check upgrade may have only two
    fields; in that case ``owner_id`` is returned as an empty string
    and the caller rejects the interaction (fail-closed).
    """
    if not interaction.message or not interaction.message.embeds:
        return None
    footer = interaction.message.embeds[0].footer.text
    if not footer or "|" not in footer:
        return None
    parts = footer.split("|", 2)
    ws_id = parts[0]
    correlation_id = parts[1] if len(parts) > 1 else ""
    owner_id = parts[2] if len(parts) > 2 else ""
    return ws_id, correlation_id, owner_id


async def _deny_non_owner(interaction: discord.Interaction, verb: str) -> None:
    """Reply with an ephemeral non-owner rejection."""
    await interaction.response.send_message(
        f"Only the session owner can {verb} this.",
        ephemeral=True,
    )


async def disable_message_buttons(message: discord.Message, label: str) -> None:
    """Disable all buttons on *message* and append *label* to the embed title.

    Used both from interaction callbacks (via the message attribute) and
    from bot event handlers when the server resolves an approval externally
    (e.g. timeout).
    """
    import discord

    view = discord.ui.View()
    for item in message.components or []:
        for child in item.children:  # type: ignore[union-attr]
            button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
                label=getattr(child, "label", ""),
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id=getattr(child, "custom_id", None),
            )
            view.add_item(button)

    embed = message.embeds[0] if message.embeds else None
    if embed is not None:
        embed.color = discord.Color.greyple()
        embed.title = f"{embed.title} - {label}"

    await message.edit(embed=embed, view=view)


async def _disable_buttons(interaction: discord.Interaction, label: str) -> None:
    """Edit the interaction message to disable all buttons and append *label* to the embed title."""
    if interaction.message is None:
        return
    await disable_message_buttons(interaction.message, label)


# ---------------------------------------------------------------------------
# ApprovalView
# ---------------------------------------------------------------------------


class ApprovalView:
    """Persistent view with Approve / Reject / Always Approve buttons."""

    def __init__(self, bot: TurnstoneBot) -> None:
        import discord

        self.bot = bot
        view_self = self

        class _View(discord.ui.View):
            def __init__(inner_self) -> None:  # noqa: N805
                super().__init__(timeout=None)

            @discord.ui.button(
                label="Approve",
                style=discord.ButtonStyle.green,
                custom_id="ts:approve",
            )
            async def approve(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle(interaction, approved=True, always=False)

            @discord.ui.button(
                label="Reject",
                style=discord.ButtonStyle.red,
                custom_id="ts:reject",
            )
            async def reject(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle(interaction, approved=False, always=False)

            @discord.ui.button(
                label="Always Approve",
                style=discord.ButtonStyle.secondary,
                custom_id="ts:always",
            )
            async def always_approve(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle(interaction, approved=True, always=True)

        self._view = _View()

    async def _handle(
        self,
        interaction: discord.Interaction,
        *,
        approved: bool,
        always: bool,
    ) -> None:
        """Process an approval button click."""
        parsed = _parse_footer(interaction)
        if parsed is None:
            await interaction.response.send_message(
                "Could not determine workstream context.",
                ephemeral=True,
            )
            return

        import asyncio

        ws_id, correlation_id, owner_id = parsed
        clicker_id = str(interaction.user.id)

        # Identity resolution: the clicker's own /link takes precedence; a
        # global-linked guild lets ANY member act as the shared guild user.
        member_link = await self.bot.router.resolve_user("discord", clicker_id)
        guild_link = None
        if interaction.guild_id is not None:
            guild_link = await asyncio.to_thread(
                self.bot.storage.get_channel_user, "guild", str(interaction.guild_id)
            )

        # Must be reachable at all: individually linked, or in a global-linked
        # guild (where everyone is treated as the guild user by default).
        if member_link is None and guild_link is None:
            await interaction.response.send_message(
                "Your Discord account is not linked. Use `/link` first, "
                "or ask an admin to use `/global-link`.",
                ephemeral=True,
            )
            return

        # Owner boundary: approvals ride the gateway's service-scoped JWT (which
        # short-circuits server-side scope checks), so the adapter is the only
        # place this can be enforced.  In a global-linked guild the members
        # share one trusted identity, so anyone may approve communally;
        # otherwise only the workstream's own invoker may.
        is_owner = bool(owner_id) and clicker_id == owner_id
        if not is_owner and guild_link is None:
            verb = "always-approve" if always else ("approve" if approved else "reject")
            await _deny_non_owner(interaction, verb)
            return

        # Defer before doing async work so Discord doesn't time out.
        await interaction.response.defer(ephemeral=True)

        await self.bot.router.send_approval(
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
            always=always,
        )

        label = "Always Approved" if always else ("Approved" if approved else "Rejected")
        # Pop pending approval so ApprovalResolvedEvent doesn't double-update.
        # Keyed by (ws_id, cycle_id); a legacy footer without a cycle_id
        # clears the ws's single tracked entry.
        pop_cycle_entry(self.bot._pending_approval_msgs, ws_id, correlation_id)
        await _disable_buttons(interaction, label)
        await interaction.followup.send(
            f"Tool execution **{label.lower()}**.",
            ephemeral=True,
        )
        log.info(
            "discord.approval_response",
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
            always=always,
        )
