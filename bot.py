from __future__ import annotations

import os
import time
import threading
from collections import defaultdict, deque
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# =========================
# Cấu hình
# =========================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("COMMAND_PREFIX", "!").strip() or "!"

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or "0")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0") or "0")
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0") or "0")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or "0")

ANTI_NUKE_THRESHOLD = max(1, int(os.getenv("ANTI_NUKE_THRESHOLD", "3") or "3"))
ANTI_NUKE_WINDOW = max(1, int(os.getenv("ANTI_NUKE_WINDOW", "20") or "20"))

if not TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN. Hãy thêm biến môi trường DISCORD_TOKEN trong Render."
    )

# =========================
# Khởi tạo bot
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.moderation = True

# Key: (server ID, user ID, action name)
action_history: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)


def configured(value: int) -> bool:
    return value > 0


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
        return True
    return configured(STAFF_ROLE_ID) and any(role.id == STAFF_ROLE_ID for role in member.roles)


async def send_log(guild: discord.Guild, message: str) -> None:
    """Gửi log nếu đã cấu hình kênh log và bot có quyền gửi."""
    if not configured(LOG_CHANNEL_ID):
        return

    channel = guild.get_channel(LOG_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        await channel.send(message[:1900])
    except (discord.Forbidden, discord.HTTPException):
        pass


async def resolve_audit_actor(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int | None = None,
) -> discord.User | discord.Member | None:
    """Tìm người thực hiện từ Audit Log; kết quả chỉ mang tính gần đúng."""
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            if age > 10:
                continue

            if target_id is not None:
                entry_target_id = getattr(entry.target, "id", None)
                if entry_target_id != target_id:
                    continue

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
            "⚠️ **DEX Anti-Nuke:** Không xác định được người thực hiện "
            f"`{action_name}` với `{target_name}`. Hãy kiểm tra Audit Log.",
        )
        return

    now = time.monotonic()
    key = (guild.id, actor.id, action_name)
    history = action_history[key]
    history.append(now)

    while history and now - history[0] > ANTI_NUKE_WINDOW:
        history.popleft()

    count = len(history)
    message = (
        f"🛡️ **DEX Anti-Nuke — cảnh báo**\n"
        f"Người thực hiện: {actor.mention} (`{actor.id}`)\n"
        f"Hành động: `{action_name}`\n"
        f"Mục tiêu: `{target_name}`\n"
        f"Số lần trong {ANTI_NUKE_WINDOW} giây: **{count}**"
    )

    if count >= ANTI_NUKE_THRESHOLD:
        message += (
            "\n🚨 **Đã chạm ngưỡng cảnh báo.** DEX chỉ cảnh báo, không tự động "
            "ban/kick. Hãy xác minh Audit Log trước khi xử lý."
        )

    await send_log(guild, message)


# =========================
# HTTP health check cho Render Web Service
# =========================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/health"):
            body = b"Not Found"
            self.send_response(404)
        else:
            body = b"DEX is running"
            self.send_response(200)

        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Không tạo log cho mỗi lần Render kiểm tra health.
        return


def start_http_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"DEX HTTP health server đang lắng nghe cổng {port}", flush=True)
    server.serve_forever()


# =========================
# Khởi động và sự kiện
# =========================
class DEXBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Đăng ký lại view persistent sau mỗi lần bot khởi động.
        self.add_view(TicketView())
        try:
            synced = await self.tree.sync()
            print(f"Đã đồng bộ {len(synced)} slash command.", flush=True)
        except discord.HTTPException as exc:
            print(f"Không đồng bộ được slash command: {exc}", flush=True)


# Tạo lại bot với lớp hỗ trợ setup_hook.
bot = DEXBot(
    command_prefix=commands.when_mentioned_or(PREFIX),
    intents=intents,
    help_command=None,
)


@bot.event
async def on_ready() -> None:
    print(
        f"DEX đã đăng nhập: {bot.user} "
        f"(ID: {bot.user.id if bot.user else 'N/A'})",
        flush=True,
    )


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if configured(WELCOME_CHANNEL_ID):
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    f"👋 Chào mừng {member.mention} đến với **{member.guild.name}**!"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    await send_log(
        member.guild,
        f"📥 Thành viên tham gia: {discord.utils.escape_markdown(str(member))} (`{member.id}`)",
    )


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await send_log(
        member.guild,
        f"📤 Thành viên rời server: {discord.utils.escape_markdown(str(member))} (`{member.id}`)",
    )


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    actor = await resolve_audit_actor(guild, discord.AuditLogAction.ban, user.id)
    await report_dangerous_action(guild, actor, "ban", f"{user} ({user.id})")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    actor = await resolve_audit_actor(
        channel.guild, discord.AuditLogAction.channel_delete, channel.id
    )
    await report_dangerous_action(
        channel.guild, actor, "channel_delete", f"{channel.name} ({channel.id})"
    )


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    actor = await resolve_audit_actor(
        role.guild, discord.AuditLogAction.role_delete, role.id
    )
    await report_dangerous_action(
        role.guild, actor, "role_delete", f"{role.name} ({role.id})"
    )


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.guild and not message.author.bot:
        content = message.content.strip() if message.content else "[không có nội dung văn bản]"
        content = discord.utils.escape_markdown(content[:700])
        await send_log(
            message.guild,
            f"🗑️ Tin nhắn bị xóa tại {message.channel.mention} "
            f"của {message.author.mention}:\n{content}",
        )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if (
        before.guild
        and not before.author.bot
        and before.content != after.content
    ):
        old = discord.utils.escape_markdown((before.content or "[trống]")[:500])
        new = discord.utils.escape_markdown((after.content or "[trống]")[:500])
        await send_log(
            before.guild,
            f"✏️ Tin nhắn được sửa tại {before.channel.mention} "
            f"bởi {before.author.mention}.\nTrước: {old}\nSau: {new}",
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.guild and not message.author.bot:
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


# =========================
# Kiểm tra quyền lệnh
# =========================
def moderator_only():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            raise commands.NoPrivateMessage()

        perms = ctx.author.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            raise commands.MissingPermissions(["manage_messages"])

        return True

    return commands.check(predicate)


# =========================
# Lệnh prefix
# =========================
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
    embed.add_field(name=f"{PREFIX}clear số_lượng", value="Xóa tối đa 100 tin nhắn.", inline=False)
    embed.add_field(name="/ticket", value="Tạo ticket hỗ trợ riêng.", inline=False)
    embed.add_field(name="/close_ticket", value="Đóng ticket hiện tại.", inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.reply(
        f"🏓 Pong! `{round(bot.latency * 1000)} ms`",
        mention_author=False,
    )


@bot.command()
@moderator_only()
async def warn(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "Không nêu lý do",
) -> None:
    if member.bot:
        await ctx.reply("Không thể cảnh báo bot bằng lệnh này.", mention_author=False)
        return

    safe_reason = discord.utils.escape_markdown(reason[:500])
    await ctx.send(f"⚠️ {member.mention} đã được cảnh báo. Lý do: **{safe_reason}**")
    await send_log(
        ctx.guild,
        f"⚠️ {ctx.author.mention} cảnh báo {member.mention}. Lý do: {reason[:700]}",
    )


def hierarchy_blocked(ctx: commands.Context, member: discord.Member) -> bool:
    if not isinstance(ctx.author, discord.Member):
        return True
    if member.id == ctx.author.id:
        return True
    if bot.user is not None and member.id == bot.user.id:
        return True
    if member.top_role >= ctx.author.top_role:
        return True
    guild_me = ctx.guild.me if ctx.guild else None
    if guild_me is None or member.top_role >= guild_me.top_role:
        return True
    return False


@bot.command()
@moderator_only()
async def timeout(
    ctx: commands.Context,
    member: discord.Member,
    minutes: int,
    *,
    reason: str = "Không nêu lý do",
) -> None:
    if minutes < 1 or minutes > 40320:
        await ctx.reply("Số phút phải từ 1 đến 40320 (28 ngày).", mention_author=False)
        return
    if hierarchy_blocked(ctx, member):
        await ctx.reply("Không thể timeout người có role ngang/cao hơn bạn hoặc bot.", mention_author=False)
        return

    try:
        await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason[:400]}")
        await ctx.reply(f"🔇 Đã timeout {member.mention} trong {minutes} phút.", mention_author=False)
        await send_log(
            ctx.guild,
            f"🔇 {ctx.author.mention} timeout {member.mention} {minutes} phút. Lý do: {reason[:700]}",
        )
    except discord.Forbidden:
        await ctx.reply("DEX thiếu quyền Timeout Members hoặc role bot quá thấp.", mention_author=False)
    except discord.HTTPException:
        await ctx.reply("Discord từ chối thao tác. Hãy thử lại sau.", mention_author=False)


@bot.command()
@moderator_only()
async def kick(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "Không nêu lý do",
) -> None:
    if hierarchy_blocked(ctx, member):
        await ctx.reply("Không thể kick người có role ngang/cao hơn bạn hoặc bot.", mention_author=False)
        return

    try:
        await member.kick(reason=f"{ctx.author}: {reason[:400]}")
        await ctx.reply(f"👢 Đã kick `{member}`. Lý do: {reason[:300]}", mention_author=False)
        await send_log(ctx.guild, f"👢 {ctx.author.mention} kick `{member}`. Lý do: {reason[:700]}")
    except discord.Forbidden:
        await ctx.reply("DEX thiếu quyền Kick Members hoặc role bot quá thấp.", mention_author=False)
    except discord.HTTPException:
        await ctx.reply("Discord từ chối thao tác. Hãy thử lại sau.", mention_author=False)


@bot.command()
@moderator_only()
async def ban(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "Không nêu lý do",
) -> None:
    if hierarchy_blocked(ctx, member):
        await ctx.reply("Không thể ban người có role ngang/cao hơn bạn hoặc bot.", mention_author=False)
        return

    try:
        await member.ban(reason=f"{ctx.author}: {reason[:400]}", delete_message_seconds=0)
        await ctx.reply(f"🔨 Đã ban `{member}`. Lý do: {reason[:300]}", mention_author=False)
        await send_log(ctx.guild, f"🔨 {ctx.author.mention} ban `{member}`. Lý do: {reason[:700]}")
    except discord.Forbidden:
        await ctx.reply("DEX thiếu quyền Ban Members hoặc role bot quá thấp.", mention_author=False)
    except discord.HTTPException:
        await ctx.reply("Discord từ chối thao tác. Hãy thử lại sau.", mention_author=False)


@bot.command()
@moderator_only()
async def clear(ctx: commands.Context, amount: int) -> None:
    if amount < 1 or amount > 100:
        await ctx.reply("Số lượng phải từ 1 đến 100.", mention_author=False)
        return
    if not isinstance(ctx.channel, discord.TextChannel):
        await ctx.reply("Lệnh này chỉ dùng trong kênh văn bản.", mention_author=False)
        return

    try:
        deleted = await ctx.channel.purge(limit=amount + 1)
        note = await ctx.send(f"🧹 Đã xóa {max(0, len(deleted) - 1)} tin nhắn.")
        await note.delete(delay=5)
    except discord.Forbidden:
        await ctx.reply("DEX cần quyền Manage Messages và Read Message History.", mention_author=False)
    except discord.HTTPException:
        await ctx.reply("Không thể xóa tin nhắn lúc này.", mention_author=False)


# =========================
# Ticket
# =========================
class TicketView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Đóng ticket",
        style=discord.ButtonStyle.danger,
        custom_id="dex:ticket:close",
    )
    async def close_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Kênh không hợp lệ.", ephemeral=True)
            return

        topic = channel.topic or ""
        owner_id = topic.split(":", 1)[1] if topic.startswith("dex-ticket-owner:") else ""
        user_is_staff = isinstance(interaction.user, discord.Member) and is_staff(interaction.user)

        if str(interaction.user.id) != owner_id and not user_is_staff:
            await interaction.response.send_message(
                "Chỉ người tạo ticket hoặc staff được đóng.", ephemeral=True
            )
            return

        await interaction.response.send_message("Ticket sẽ được đóng sau 3 giây.")
        await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=3))
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            pass


@bot.tree.command(name="ticket", description="Tạo ticket hỗ trợ riêng với đội ngũ quản trị.")
@app_commands.guild_only()
async def ticket(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Lệnh này chỉ dùng trong server.", ephemeral=True
        )
        return

    existing = discord.utils.find(
        lambda ch: isinstance(ch, discord.TextChannel)
        and (ch.topic or "") == f"dex-ticket-owner:{interaction.user.id}",
        guild.text_channels,
    )
    if existing:
        await interaction.response.send_message(
            f"Bạn đã có ticket: {existing.mention}", ephemeral=True
        )
        return

    category = guild.get_channel(TICKET_CATEGORY_ID) if configured(TICKET_CATEGORY_ID) else None
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        ),
    }

    bot_member = guild.me
    if bot_member is not None:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )

    # Cho phép role staff đã cấu hình xem ticket.
    if configured(STAFF_ROLE_ID):
        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role is not None:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
            )

    try:
        safe_name = "".join(
            char.lower() if char.isalnum() else "-"
            for char in interaction.user.name
        ).strip("-")
        safe_name = "-".join(part for part in safe_name.split("-") if part)[:70] or "member"

        channel = await guild.create_text_channel(
            name=f"ticket-{safe_name}"[:90],
            category=category,
            overwrites=overwrites,
            topic=f"dex-ticket-owner:{interaction.user.id}",
            reason=f"Ticket created by {interaction.user}",
        )
        await channel.send(
            f"{interaction.user.mention} hãy mô tả vấn đề tại đây. "
            "Staff có quyền truy cập ticket sẽ hỗ trợ bạn.",
            view=TicketView(),
        )
        await interaction.response.send_message(
            f"Đã tạo ticket: {channel.mention}", ephemeral=True
        )
        await send_log(
            guild,
            f"🎫 Ticket được tạo bởi {interaction.user.mention}: {channel.mention}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "DEX thiếu quyền Manage Channels hoặc quyền tạo/xem kênh.", ephemeral=True
        )
    except discord.HTTPException:
        await interaction.response.send_message(
            "Không tạo được ticket, hãy thử lại.", ephemeral=True
        )


@bot.tree.command(name="close_ticket", description="Đóng ticket hiện tại.")
@app_commands.guild_only()
async def close_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "Bạn cần dùng lệnh trong kênh ticket.", ephemeral=True
        )
        return

    topic = channel.topic or ""
    if not topic.startswith("dex-ticket-owner:"):
        await interaction.response.send_message(
            "Bạn cần dùng lệnh trong kênh ticket.", ephemeral=True
        )
        return

    owner_id = topic.split(":", 1)[1]
    user_is_staff = isinstance(interaction.user, discord.Member) and is_staff(interaction.user)
    if str(interaction.user.id) != owner_id and not user_is_staff:
        await interaction.response.send_message(
            "Chỉ người tạo ticket hoặc staff được đóng.", ephemeral=True
        )
        return

    await interaction.response.send_message("Ticket sẽ được đóng sau 3 giây.")
    await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=3))
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException):
        pass


# =========================
# Xử lý lỗi lệnh
# =========================
@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        message = "Bạn không có quyền dùng lệnh này."
    elif isinstance(error, commands.MissingRequiredArgument):
        message = f"Thiếu tham số: `{error.param.name}`. Dùng `{PREFIX}helpdex`."
    elif isinstance(error, commands.BadArgument):
        message = "Tham số không hợp lệ. Hãy kiểm tra mention và số liệu."
    elif isinstance(error, commands.NoPrivateMessage):
        message = "Lệnh này chỉ dùng trong server."
    else:
        original = getattr(error, "original", error)
        print(f"Lỗi lệnh {ctx.command}: {original!r}", flush=True)
        message = "Có lỗi khi chạy lệnh. Hãy kiểm tra log của Render."

    try:
        await ctx.reply(message, mention_author=False)
    except (discord.Forbidden, discord.HTTPException):
        pass


# =========================
# Chạy chương trình
# =========================
if __name__ == "__main__":
    # HTTP server chạy nền để Render Web Service nhận diện cổng.
    threading.Thread(target=start_http_server, daemon=True, name="dex-health").start()
    bot.run(TOKEN, log_handler=None)