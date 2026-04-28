import os
import json
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import Database
from server import start_web_server

# 環境変数と設定の読み込み
DATABASE_URL = os.environ.get("DATABASE_URL")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
JST = ZoneInfo("Asia/Tokyo")

with open('config.json', 'r', encoding='utf-8') as f:
    CONFIG = json.load(f)

# Bot初期化
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

class AttendanceBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.db = Database(DATABASE_URL)
        self.scheduler = AsyncIOScheduler(timezone=JST)

    async def setup_hook(self):
        await self.db.connect()
        await self.tree.sync()
        self.scheduler.add_job(midnight_batch_process, CronTrigger(hour=0, minute=0, timezone=JST))
        self.scheduler.start()

bot = AttendanceBot()

# --- VCイベント検知 ---
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    now = datetime.now(JST)

    if before.channel is None and after.channel is not None:
        await bot.db.set_vc_join(member.id, member.guild.id, now)

    elif before.channel is not None and after.channel is None:
        records = await bot.db.get_all_current_vc()
        user_record = next((r for r in records if r['user_id'] == member.id), None)
        
        if user_record:
            join_time = user_record['join_time'].astimezone(JST)
            if join_time.date() == now.date():
                duration = int((now - join_time).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, now.date(), duration)
            else:
                end_of_join_day = datetime.combine(join_time.date() + timedelta(days=1), datetime.min.time(), tzinfo=JST)
                duration_day1 = int((end_of_join_day - join_time).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, join_time.date(), duration_day1)
                
                start_of_leave_day = datetime.combine(now.date(), datetime.min.time(), tzinfo=JST)
                duration_day2 = int((now - start_of_leave_day).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, now.date(), duration_day2)

            await bot.db.remove_vc_join(member.id)

# --- 0時バッチ処理 ---
async def midnight_batch_process():
    print(f"\n--- [0時バッチ処理開始] {datetime.now(JST)} ---")
    now = datetime.now(JST)
    yesterday = (now - timedelta(days=1)).date()

    current_vc_users = await bot.db.get_all_current_vc()
    for record in current_vc_users:
        user_id = record['user_id']
        guild_id = record['guild_id']
        join_time = record['join_time'].astimezone(JST)

        duration = int((now - join_time).total_seconds() // 60)
        await bot.db.add_daily_time(user_id, guild_id, yesterday, duration)
        await bot.db.set_vc_join(user_id, guild_id, now)

    for guild in bot.guilds:
        threshold = await bot.db.get_threshold(guild.id)
        for member in guild.members:
            if member.bot:
                continue
            # バッチ処理時はログを標準出力に流す
            await update_member_role(member, guild, yesterday, threshold)
    print("--- [0時バッチ処理終了] ---\n")


# --- 出席率計算・ロール更新ロジック ---
def get_total_valid_days(start_date: date, end_date: date) -> int:
    valid_days = 0
    current = start_date
    weekdays_exclude = CONFIG['exclude_days']['weekdays']
    holidays_exclude =[datetime.strptime(d, "%Y-%m-%d").date() for d in CONFIG['exclude_days']['holidays']]

    while current <= end_date:
        if current.weekday() not in weekdays_exclude and current not in holidays_exclude:
            valid_days += 1
        current += timedelta(days=1)
    return valid_days

async def calculate_attendance(member: discord.Member, guild: discord.Guild, target_date: date, threshold: int):
    records = await bot.db.get_user_attendance(member.id, guild.id)
    bot_start = datetime.strptime(CONFIG['bot_start_date'], "%Y-%m-%d").date()
    member_join = member.joined_at.astimezone(JST).date() if member.joined_at else bot_start

    if records:
        oldest_record_date = min(r['record_date'] for r in records)
        member_start = min(member_join, oldest_record_date)
    else:
        member_start = member_join

    start_date = max(bot_start, member_start)

    if target_date < start_date:
        return 0, 0, 0

    total_valid_days = get_total_valid_days(start_date, target_date)
    if total_valid_days == 0:
        return 0, 0, 0

    attended_days = 0
    weekdays_exclude = CONFIG['exclude_days']['weekdays']
    holidays_exclude = [datetime.strptime(d, "%Y-%m-%d").date() for d in CONFIG['exclude_days']['holidays']]

    for r in records:
        r_date = r['record_date']
        if r_date.weekday() in weekdays_exclude or r_date in holidays_exclude:
            continue
        if r_date < start_date or r_date > target_date:
            continue

        if r['is_override']:
            if r['override_status'] == 'attended':
                attended_days += 1
        else:
            if r['total_minutes'] >= threshold:
                attended_days += 1

    rate = (attended_days / total_valid_days) * 100
    return min(rate, 100.0), attended_days, total_valid_days

async def update_member_role(member: discord.Member, guild: discord.Guild, target_date: date, threshold: int):
    """
    メンバーのロールを更新し、結果と詳細なログメッセージを返す
    """
    rate, attended, total = await calculate_attendance(member, guild, target_date, threshold)

    target_role_name = None
    for role_cfg in sorted(CONFIG['roles'], key=lambda x: x['min_percent'], reverse=True):
        if rate >= role_cfg['min_percent']:
            target_role_name = role_cfg['name']
            break

    all_role_names = [r['name'] for r in CONFIG['roles']]
    roles_to_remove = []
    role_to_add = None
    server_has_target_role = False

    for r in guild.roles:
        if r.name in all_role_names:
            if r.name == target_role_name:
                role_to_add = r
                server_has_target_role = True
            else:
                roles_to_remove.append(r)

    # --- ログメッセージ構築開始 ---
    log_messages = []
    log_messages.append(f"対象ロール: {target_role_name or '該当なし'}")

    if target_role_name and not server_has_target_role:
        log_messages.append(f"⚠️ エラー: '{target_role_name}' という名前のロールがサーバーに存在しません。config.jsonと完全に一致しているか確認してください。")

    # 実際に剥奪・付与が必要なロールだけを抽出
    actual_roles_to_remove = [r for r in roles_to_remove if r in member.roles]
    needs_update = False

    if actual_roles_to_remove:
        needs_update = True
    if role_to_add and role_to_add not in member.roles:
        needs_update = True

    if not needs_update:
        if target_role_name and server_has_target_role:
            log_messages.append("ℹ️ ロールの変更は不要です (既に付与済み)")
        elif not target_role_name:
            log_messages.append("ℹ️ ロールの変更は不要です (基準に未達)")
        
        final_log = "\n".join(log_messages)
        print(f"[{member.display_name}] {final_log}")
        return rate, attended, total, final_log

    # --- 実際のロール付与・剥奪処理 ---
    try:
        if actual_roles_to_remove:
            await member.remove_roles(*actual_roles_to_remove, reason="出席率システム: 古いロールの剥奪")
            log_messages.append(f"✅ 剥奪成功: {', '.join([r.name for r in actual_roles_to_remove])}")

        if role_to_add and role_to_add not in member.roles:
            await member.add_roles(role_to_add, reason="出席率システム: 新しいロールの付与")
            log_messages.append(f"✅ 付与成功: {role_to_add.name}")

    except discord.Forbidden:
        err_msg = "❌ 権限エラー(Forbidden): Botのロールが対象ロールより下に配置されているか、ロール管理権限がありません。"
        log_messages.append(err_msg)
    except Exception as e:
        err_msg = f"❌ 予期せぬエラー: {str(e)}"
        log_messages.append(err_msg)

    final_log = "\n".join(log_messages)
    print(f"[{member.display_name}] {final_log}")
    return rate, attended, total, final_log


# --- スラッシュコマンド ---
@bot.tree.command(name="attendance", description="現在の出席率を確認し、ロールを更新します")
async def attendance(interaction: discord.Interaction, target_user: discord.Member = None):
    await interaction.response.defer()

    user = target_user or interaction.user
    today = datetime.now(JST).date()
    threshold = await bot.db.get_threshold(interaction.guild.id)
    
    # 計算とロール更新を同時に実行し、ログを取得
    rate, attended, total, log_msg = await update_member_role(user, interaction.guild, today, threshold)
    
    embed = discord.Embed(title=f"📊 {user.display_name} の出席率", color=discord.Color.blue())
    embed.add_field(name="出席率", value=f"**{rate:.1f}%**", inline=False)
    embed.add_field(name="出席日数 / 総日数", value=f"{attended}日 / {total}日", inline=False)
    
    # 実行結果・エラーログをEmbedに表示
    embed.add_field(name="⚙️ ロール更新ステータス", value=f"```\n{log_msg}\n```", inline=False)
    
    embed.set_footer(text=f"規定時間: 1日{threshold}分以上")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="set_threshold", description="【管理者用】出席判定の規定時間(分)を変更します")
@app_commands.default_permissions(manage_roles=True)
async def set_threshold(interaction: discord.Interaction, minutes: int):
    if minutes <= 0:
        await interaction.response.send_message("1以上の数値を指定してください。", ephemeral=True)
        return
    await bot.db.set_threshold(interaction.guild.id, minutes)
    await interaction.response.send_message(f"✅ このサーバーの出席規定時間を **{minutes}分** に変更しました。")


@bot.tree.command(name="override_attendance", description="【管理者用】過去の出席状況を強制上書きします")
@app_commands.default_permissions(manage_roles=True)
@app_commands.choices(status=[
    app_commands.Choice(name="出席 (attended)", value="attended"),
    app_commands.Choice(name="欠席 (absent)", value="absent")
])
async def override_attendance(interaction: discord.Interaction, target_user: discord.Member, target_date: str, status: app_commands.Choice[str]):
    try:
        date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        await interaction.response.send_message("❌ 日付の形式が正しくありません。`YYYY-MM-DD` で入力してください。", ephemeral=True)
        return

    records = await bot.db.get_user_attendance(target_user.id, interaction.guild.id)
    bot_start = datetime.strptime(CONFIG['bot_start_date'], "%Y-%m-%d").date()
    member_join = target_user.joined_at.astimezone(JST).date() if target_user.joined_at else bot_start

    if records:
        oldest_record_date = min(r['record_date'] for r in records)
        member_start = min(member_join, oldest_record_date)
    else:
        member_start = member_join

    start_date = max(bot_start, member_start)

    if date_obj < start_date:
        await interaction.response.send_message("❌ 参加前、またはBot導入前の日付のため変更できません。", ephemeral=True)
        return

    await bot.db.set_override(target_user.id, interaction.guild.id, date_obj, status.value)
    
    threshold = await bot.db.get_threshold(interaction.guild.id)
    # 上書き後にロールを再計算して更新
    _, _, _, log_msg = await update_member_role(target_user, interaction.guild, datetime.now(JST).date(), threshold)

    await interaction.response.send_message(f"✅ {target_user.display_name} の `{target_date}` の記録を **{status.name}** に上書きしました。\n```\n{log_msg}\n```")


if __name__ == "__main__":
    start_web_server()
    bot.run(DISCORD_TOKEN)