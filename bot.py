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
        # DB接続
        await self.db.connect()
        # スラッシュコマンドの同期
        await self.tree.sync()
        # 0時バッチ処理のスケジュール登録 (JST 0:00)
        self.scheduler.add_job(midnight_batch_process, CronTrigger(hour=0, minute=0, timezone=JST))
        self.scheduler.start()
        
        # [リカバリ処理] Botダウン中に0時を過ぎた場合の補正などを行えますが、
        # 今回は無料枠での即時復旧を優先し、起動時点でVCにいる人のjoin_timeを更新します。
        # (厳密にはここで現在のVC参加者をスキャンしDBと照合する処理が理想ですが、ここでは割愛しDB依存とします)

bot = AttendanceBot()

# --- VCイベント検知 ---
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    now = datetime.now(JST)

    # 入室した時（前がNone、後がVC）
    if before.channel is None and after.channel is not None:
        await bot.db.set_vc_join(member.id, member.guild.id, now)

    # 退室した時（前がVC、後がNone）
    elif before.channel is not None and after.channel is None:
        records = await bot.db.get_all_current_vc()
        user_record = next((r for r in records if r['user_id'] == member.id), None)
        
        if user_record:
            join_time = user_record['join_time'].astimezone(JST)
            # 同じ日に退室した場合
            if join_time.date() == now.date():
                duration = int((now - join_time).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, now.date(), duration)
            else:
                # 入室日と退室日が異なる場合（本来は0時バッチで処理されるが、Botダウン等の保険）
                # 入室日から0時まで
                end_of_join_day = datetime.combine(join_time.date() + timedelta(days=1), datetime.min.time(), tzinfo=JST)
                duration_day1 = int((end_of_join_day - join_time).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, join_time.date(), duration_day1)
                
                # 0時から退室まで
                start_of_leave_day = datetime.combine(now.date(), datetime.min.time(), tzinfo=JST)
                duration_day2 = int((now - start_of_leave_day).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, now.date(), duration_day2)

            await bot.db.remove_vc_join(member.id)

    # チャンネル移動・ミュート等の状態変化（VC内に留まっている場合）は何もしない


# --- 0時バッチ処理 ---
async def midnight_batch_process():
    now = datetime.now(JST) # 0:00 just
    yesterday = (now - timedelta(days=1)).date()

    # 1. 0時またぎのVC滞在時間精算
    current_vc_users = await bot.db.get_all_current_vc()
    for record in current_vc_users:
        user_id = record['user_id']
        guild_id = record['guild_id']
        join_time = record['join_time'].astimezone(JST)

        # 昨日分の時間を計算して加算
        duration = int((now - join_time).total_seconds() // 60)
        await bot.db.add_daily_time(user_id, guild_id, yesterday, duration)
        
        # 新しい入室時刻を0時0分としてDBを更新（仮想的な再入室）
        await bot.db.set_vc_join(user_id, guild_id, now)

    # 2. 全ユーザーのロール更新処理
    # (注意: 全ギルド、全メンバーをループするため大規模サーバーでは非同期タスク分割が必要)
    for guild in bot.guilds:
        threshold = await bot.db.get_threshold(guild.id)
        for member in guild.members:
            if member.bot:
                continue
            await update_member_role(member, guild, yesterday, threshold)


# --- 出席率計算・ロール更新ロジック ---
def get_total_valid_days(start_date: date, end_date: date) -> int:
    """開始日から終了日までのうち、休日を除外した有効日数を計算"""
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
    # 開始日の決定（Bot導入日 or ユーザー参加日の遅い方）
    bot_start = datetime.strptime(CONFIG['bot_start_date'], "%Y-%m-%d").date()
    member_join = member.joined_at.astimezone(JST).date() if member.joined_at else bot_start
    start_date = max(bot_start, member_join)

    # 参加前なら計算不可
    if target_date < start_date:
        return 0, 0, 0

    total_valid_days = get_total_valid_days(start_date, target_date)
    if total_valid_days == 0:
        return 0, 0, 0

    records = await bot.db.get_user_attendance(member.id, guild.id)
    attended_days = 0

    weekdays_exclude = CONFIG['exclude_days']['weekdays']
    holidays_exclude = [datetime.strptime(d, "%Y-%m-%d").date() for d in CONFIG['exclude_days']['holidays']]

    for r in records:
        r_date = r['record_date']
        # 休日の記録は無視する（100%超えバグ防止）
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
    rate, _, _ = await calculate_attendance(member, guild, target_date, threshold)

    # 付与すべきロールを判定
    target_role_name = None
    for role_cfg in sorted(CONFIG['roles'], key=lambda x: x['min_percent'], reverse=True):
        if rate >= role_cfg['min_percent']:
            target_role_name = role_cfg['name']
            break

    # Discordサーバー上のロールオブジェクトを取得
    all_role_names = [r['name'] for r in CONFIG['roles']]
    roles_to_remove =[]
    role_to_add = None

    for r in guild.roles:
        if r.name in all_role_names:
            if r.name == target_role_name:
                role_to_add = r
            else:
                roles_to_remove.append(r)

    # 権限エラーを避けるため、try-exceptで囲む
    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="出席率バッチ処理: 古いロールの剥奪")
        if role_to_add and role_to_add not in member.roles:
            await member.add_roles(role_to_add, reason="出席率バッチ処理: 新しいロールの付与")
    except discord.Forbidden:
        print(f"ロール変更権限がありません: {guild.name}")


# --- スラッシュコマンド ---
@bot.tree.command(name="attendance", description="現在の出席率を確認します")
async def attendance(interaction: discord.Interaction, target_user: discord.Member = None):
    user = target_user or interaction.user
    today = datetime.now(JST).date()
    threshold = await bot.db.get_threshold(interaction.guild.id)
    
    rate, attended, total = await calculate_attendance(user, interaction.guild, today, threshold)
    
    embed = discord.Embed(title=f"📊 {user.display_name} の出席率", color=discord.Color.blue())
    embed.add_field(name="出席率", value=f"**{rate:.1f}%**", inline=False)
    embed.add_field(name="出席日数 / 総日数", value=f"{attended}日 / {total}日", inline=False)
    embed.set_footer(text=f"規定時間: 1日{threshold}分以上")
    await interaction.response.send_message(embed=embed)


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

    # 日付の検証（導入日・参加日以前のチェック）
    bot_start = datetime.strptime(CONFIG['bot_start_date'], "%Y-%m-%d").date()
    member_join = target_user.joined_at.astimezone(JST).date() if target_user.joined_at else bot_start
    start_date = max(bot_start, member_join)

    if date_obj < start_date:
        await interaction.response.send_message("❌ 参加前、またはBot導入前の日付のため変更できません。", ephemeral=True)
        return

    await bot.db.set_override(target_user.id, interaction.guild.id, date_obj, status.value)
    
    # ロールの即時再計算
    threshold = await bot.db.get_threshold(interaction.guild.id)
    await update_member_role(target_user, interaction.guild, datetime.now(JST).date(), threshold)

    await interaction.response.send_message(f"✅ {target_user.display_name} の `{target_date}` の記録を **{status.name}** に上書きしました。")


if __name__ == "__main__":
    # Uptime Robot用Webサーバーを別スレッドで起動
    start_web_server()
    # Bot起動
    bot.run(DISCORD_TOKEN)