from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("COMMAND_PREFIX", "!").strip() or "!"
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or "0")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0") or "0")
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0") or "0")
ANTI_NUKE_THRESHOLD = int(os.getenv("ANTI_NUKE_THRESHOLD", "3") or "3")
ANTI_NUKE_WINDOW = int(os.getenv("ANTI_NUKE_WINDOW", "20") or "20")

if not TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN. Hãy thêm biến môi trường DISCORD_TOKEN trên Render."
    )

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or(PREFIX), intents=intents)
action_history: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)


def is_configured_id(value: int) -> bool:
    return value > 0


async def send_log(guild: discord.Guild, message: str) -> None:
    """Send a log message if a log channel is configured and accessible."""
    if not is_configured_id(LOG_CHANNEL_ID):
        return
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(message[:1900])
        except discord.HTTPException:
            pass


async def resolve_audit_actor(
    guild: discord.Guild, action: discord.AuditLogAction
) -> discord.User | discord.Member | None:
    try:
        async for entry in guild.audit_logs(limit=5, action=action):
            # Discord audit log entries are recent; event-specific matching is
            # best-effort because Discord does not expose a universal event ID.
            if (discord.utils.utcnow() - entry.created_at).total_seconds() <= 8:
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


async def report_dangerous_action(
    guild: discord.Guild,
    actor: discord.User | discord.Member | None,
    action_name: str,
    target_name: str,
) -> None:
    if actor is None:
        await send_log(
            guild,
            f"⚠️ **DEX Anti-Nuke:** Không xác định được người thực hiện "
            f"`{action_name}` với `{target_name}`. Hãy kiểm tra Audit Log.",
        )
        return

    now = time.monotonic()
    key = (guild.id, actor.id, action_name)
    history = action_history[key]
    history.append(now)
    while history and now - history[0] > ANTI_NUKE_WINDOW:
        history.popleft()

    message = (
        f"🛡️ **DEX Anti-Nuke — cảnh báo**\n"
        f"Người thực hiện: {actor.mention} (`{actor.id}`)\n"
        f"Hành động: `{action_name}`\n"
        f"Mục tiêu: `{target_name}`\n"
        f"Số lần trong {ANTI_NUKE_WINDOW}s: **{len(history)}**"
    )
    if len(history) >= ANTI_NUKE_THRESHOLD:
        message += (
            "\n🚨 **Đã chạm ngưỡng cảnh báo.** DEX chưa tự động ban/kick người này; "
            "hãy xác minh Audit Log và quyền của actor trước khi xử lý."
        )
    await send_log(guild, message)


@bot.event
async def on_ready() -> None:
    try:
        synced = await bot.tree.sync()
        print(f"Đã đồng bộ {len(synced)} slash command.")
    except discord.HTTPException as exc:
        print(f"Không đồng bộ được slash command: {exc}")
    print(f"DEX đã đăng nhập: {bot.user} (ID: {bot.user.id if bot.user else 'N/A'})")


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if is_configured_id(WELCOME_CHANNEL_ID):
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    f"👋 Chào mừng {member.mention} đến với **{member.guild.name}**!"
                )
            except discord.HTTPException:
                pass
    await send_log(member.guild, f"📥 Thành viên tham gia: {member} (`{member.id}`)")


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await send_log(member.guild, f"📤 Thành viên rời server: {member} (`{member.id}`)")


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    actor = await resolve_audit_actor(guild, discord.AuditLogAction.ban)
    await report_dangerous_action(guild, actor, "ban", f"{user} ({user.id})")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    actor = await resolve_audit_actor(channel.guild, discord.AuditLogAction.channel_delete)
    await report_dangerous_action(
        channel.guild, actor, "channel_delete", f"{channel.name} ({channel.id})"
    )


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    actor = await resolve_audit_actor(role.guild, discord.AuditLogAction.role_delete)
    await report_dangerous_action(
        role.guild, actor, "role_delete", f"{role.name} ({role.id})"
    )


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.guild and not message.author.bot:
        content = message.content.strip() if message.content else "[không có nội dung văn bản]"
        await send_log(
            message.guild,
            f"🗑️ Tin nhắn bị xóa tại {message.channel.mention} "
            f"bởi/thuộc {message.author.mention}: {discord.utils.escape_markdown(content[:700])}",
        )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if before.guild and not before.author.bot and before.content != after.content:
        await send_log(
            before.guild,
            f"✏️ Tin nhắn được sửa tại {before.channel.mention} "
            f"bởi {before.author.mention}.\n"
            f"Trước: {discord.utils.escape_markdown((before.content or '[trống]')[:500])}\n"
            f"Sau: {discord.utils.escape_markdown((after.content or '[trống]')[:500])}",
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.guild and not message.author.bot:
        # Basic anti-spam warning: 6 messages in 8 seconds. This only warns/logs;
        # it does not delete messages or punish members automatically.
        key = (message.guild.id, message.author.id, "chat_spam")
        now = time.monotonic()
        history = action_history[key]
        history.append(now)
        while history and now - history[0] > 8:
            history.popleft()
        if len(history) == 6:
            await send_log(
                message.guild,
                f"⚠️ Có thể spam: {message.author.mention} gửi 6 tin trong 8 giây "
                f"tại {message.channel.mention}.",
            )
    await bot.process_commands(message)


def moderator_only():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            raise commands.NoPrivateMessage()
        if not (ctx.author.guild_permissions.manage_messages or ctx.author.guild_permissions.administrator):
            raise commands.MissingPermissions(["manage_messages"])
        return True
    return commands.check(predicate)


@bot.command(name="helpdex")
async def helpdex(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title="DEX — Hướng dẫn",
        description="Các lệnh cơ bản của bot.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name=f"{PREFIX}ping", value="Kiểm tra độ trễ.", inline=False)
    embed.add_field(name=f"{PREFIX}warn @thành_viên lý_do", value="Gửi cảnh báo.", inline=False)
    embed.add_field(name=f"{PREFIX}timeout @thành_viên phút lý_do", value="Timeout thành viên.", inline=False)
    embed.add_field(name=f"{PREFIX}kick @thành_viên lý_do", value="Kick thành viên.", inline=False)
    embed.add_field(name=f"{PREFIX}ban @thành_viên lý_do", value="Ban thành viên.", inline=False)
    embed.add_field(name=f"{PREFIX}clear số_lượng", value="Xóa tin nhắn gần đây.", inline=False)
    embed.add_field(name="/ticket", value="Tạo ticket hỗ trợ riêng.", inline=False)
    embed.add_field(name="/close_ticket", value="Đóng ticket hiện tại.", inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.reply(f"🏓 Pong! `{round(bot.latency * 1000)} ms`", mention_author=False)


@bot.command()
@moderator_only()
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "Không nêu lý do") -> None:
    if member.bot:
        await ctx.reply("Không thể cảnh báo bot bằng lệnh này.")
        return
    await ctx.send(f"⚠️ {member.mention} đã được cảnh báo. Lý do: **{reason[:500]}**")
    await send_log(ctx.guild, f"⚠️ {ctx.author.mention} cảnh báo {member.mention}. Lý do: {reason[:700]}")


@bot.command()
@moderator_only()
async def timeout(
    ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "Không nêu lý do"
) -> None:
    if minutes < 1 or minutes > 40320:
        await ctx.reply("Số phút phải từ 1 đến 40320 (28 ngày).")
        return
    if member.id == ctx.author.id or member.top_role >= ctx.author.top_role:
        await ctx.reply("Không thể timeout chính bạn hoặc người có role ngang/cao hơn bạn.")
        return
    try:
        await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
        await ctx.reply(f"🔇 Đã timeout {member.mention} trong {minutes} phút.")
        await send_log(ctx.guild, f"🔇 {ctx.author.mention} timeout {member.mention} {minutes} phút. Lý do: {reason[:700]}")
    except discord.Forbidden:
        await ctx.reply("DEX thiếu quyền Timeout Members hoặc role bot quá thấp.")
    except discord.HTTPException:
        await ctx.reply("Discord từ chối thao tác. Hãy thử lại sau.")


@bot.command()
@moderator_only()
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "Không nêu lý do") -> None:
    if member.id == ctx.author.id or member.top_role >= ctx.author.top_role:
        await ctx.reply("Không thể kick chính bạn hoặc người có role ngang/cao hơn bạn.")
        return
    try:
        await member.kick(reason=f"{ctx.author}: {reason}")
        await ctx.reply(f"👢 Đã kick `{member}`. Lý do: {reason[:300]}")
        await send_log(ctx.guild, f"👢 {ctx.author.mention} kick `{member}`. Lý do: {reason[:700]}")
    except discord.Forbidden:
        await ctx.reply("DEX thiếu quyền Kick Members hoặc role bot quá thấp.")
    except discord.HTTPException:
        await ctx.reply("Discord từ chối thao tác. Hãy thử lại sau.")


@bot.command()
@moderator_only()
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "Không nêu lý do") -> None:
    if member.id == ctx.author.id or member.top_role >= ctx.author.top_role:
        await ctx.reply("Không thể ban chính bạn hoặc người có role ngang/cao hơn bạn.")
        return
    try:
        await member.ban(reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
        await ctx.reply(f"🔨 Đã ban `{member}`. Lý do: {reason[:300]}")
        await send_log(ctx.guild, f"🔨 {ctx.author.mention} ban `{member}`. Lý do: {reason[:700]}")
    except discord.Forbidden:
        await ctx.reply("DEX thiếu quyền Ban Members hoặc role bot quá thấp.")
    except discord.HTTPException:
        await ctx.reply("Discord từ chối thao tác. Hãy thử lại sau.")


@bot.command()
@moderator_only()
async def clear(ctx: commands.Context, amount: int) -> None:
    if amount < 1 or amount > 100:
        await ctx.reply("Số lượng phải từ 1 đến 100.")
        return
    if not isinstance(ctx.channel, discord.TextChannel):
        await ctx.reply("Lệnh này chỉ dùng trong kênh văn bản.")
        return
    try:
        deleted = await ctx.channel.purge(limit=amount + 1)
        note = await ctx.send(f"🧹 Đã xóa {max(0, len(deleted) - 1)} tin nhắn.")
        await note.delete(delay=5)
    except discord.Forbidden:
        await ctx.reply("DEX cần quyền Manage Messages và Read Message History.")
    except discord.HTTPException:
        await ctx.reply("Không thể xóa tin nhắn lúc này.")


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Đóng ticket", style=discord.ButtonStyle.danger, custom_id="dex:ticket:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Kênh không hợp lệ.", ephemeral=True)
            return
        is_staff = isinstance(interaction.user, discord.Member) and (
            interaction.user.guild_permissions.manage_channels
            or interaction.user.guild_permissions.administrator
        )
        if not is_staff and channel.topic != f"dex-ticket-owner:{interaction.user.id}":
            await interaction.response.send_message("Chỉ người tạo ticket hoặc staff được đóng.", ephemeral=True)
            return
        await interaction.response.send_message("Ticket sẽ được đóng sau 3 giây.")
        await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=3))
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


@bot.tree.command(name="ticket", description="Tạo ticket hỗ trợ riêng với đội ngũ quản trị.")
@app_commands.guild_only()
async def ticket(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
        return

    existing = discord.utils.get(
        guild.text_channels, topic=f"dex-ticket-owner:{interaction.user.id}"
    )
    if existing:
        await interaction.response.send_message(
            f"Bạn đã có ticket: {existing.mention}", ephemeral=True
        )
        return

    category = guild.get_channel(TICKET_CATEGORY_ID) if is_configured_id(TICKET_CATEGORY_ID) else None
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
        ),
    }
    # Staff with Manage Channels can see the ticket if their role permissions allow it.
    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}".lower().replace(" ", "-")[:90],
            category=category,
            overwrites=overwrites,
            topic=f"dex-ticket-owner:{interaction.user.id}",
            reason=f"Ticket created by {interaction.user}",
        )
        await channel.send(
            f"{interaction.user.mention} mô tả vấn đề của bạn tại đây. Staff có quyền quản lý kênh "
            f"có thể hỗ trợ bạn.",
            view=TicketView(),
        )
        await interaction.response.send_message(f"Đã tạo ticket: {channel.mention}", ephemeral=True)
        await send_log(guild, f"🎫 Ticket được tạo bởi {interaction.user.mention}: {channel.mention}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "DEX thiếu quyền Manage Channels hoặc quyền xem kênh.", ephemeral=True
        )
    except discord.HTTPException:
        await interaction.response.send_message("Không tạo được ticket, hãy thử lại.", ephemeral=True)


@bot.tree.command(name="close_ticket", description="Đóng ticket hiện tại.")
@app_commands.guild_only()
async def close_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or not (channel.topic or "").startswith("dex-ticket-owner:"):
        await interaction.response.send_message("Bạn cần dùng lệnh trong kênh ticket.", ephemeral=True)
        return
    owner_id = (channel.topic or "").split(":", 1)[1]
    is_staff = isinstance(interaction.user, discord.Member) and (
        interaction.user.guild_permissions.manage_channels
        or interaction.user.guild_permissions.administrator
    )
    if str(interaction.user.id) != owner_id and not is_staff:
        await interaction.response.send_message("Chỉ người tạo ticket hoặc staff được đóng.", ephemeral=True)
        return
    await interaction.response.send_message("Đang đóng ticket...")
    await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=3))
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except discord.HTTPException:
        pass


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("Bạn không có quyền dùng lệnh này.", mention_author=False)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Thiếu tham số: `{error.param.name}`. Dùng `{PREFIX}helpdex`.", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("Tham số không hợp lệ. Hãy kiểm tra mention và số liệu.", mention_author=False)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.reply("Lệnh này chỉ dùng trong server.", mention_author=False)
    else:
        print(f"Lỗi lệnh {ctx.command}: {error!r}")
        await ctx.reply("Có lỗi khi chạy lệnh. Hãy kiểm tra log của Render.", mention_author=False)


bot.run(TOKEN)
