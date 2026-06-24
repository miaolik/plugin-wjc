"""针对撤回插件 — 针对指定用户自动撤回消息"""

import asyncio
import json
import os
import re
import time
from urllib.parse import quote

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, interceptor, on_load

log = get_logger(PLUGIN, '针对撤回')

__plugin_meta__ = {
    'name': '针对撤回',
    'author': 'ElainaBot',
    'description': '针对指定用户自动撤回消息',
    'version': '2.0.0',
}

# ==================== 数据持久化 ====================

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_PLUGIN_DIR, 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
_TARGETS_FILE = os.path.join(_DATA_DIR, 'targets.json')

# { group_id: { user_id: expire_timestamp_or_0 } }
# expire_timestamp_or_0: 0 表示永久, >0 表示到期时间戳
_targets: dict[str, dict[str, float]] = {}

_cleanup_task = None


def _load_targets():
    global _targets
    if not os.path.isfile(_TARGETS_FILE):
        _targets = {}
        return
    try:
        with open(_TARGETS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        _targets = {}
        return
    # 兼容旧格式: { gid: [uid, ...] } -> { gid: { uid: 0 } }
    migrated = {}
    for gid, val in raw.items():
        if isinstance(val, list):
            migrated[gid] = {uid: 0 for uid in val}
        elif isinstance(val, dict):
            migrated[gid] = val
        else:
            continue
    _targets = migrated


def _save_targets():
    with open(_TARGETS_FILE, 'w', encoding='utf-8') as f:
        json.dump(_targets, f, ensure_ascii=False, indent=2)


def _purge_expired():
    """清理所有过期的针对记录"""
    now = time.time()
    changed = False
    for gid in list(_targets):
        group = _targets[gid]
        expired = [uid for uid, exp in group.items() if exp and exp <= now]
        for uid in expired:
            del group[uid]
            changed = True
        if not group:
            del _targets[gid]
    if changed:
        _save_targets()


async def _periodic_cleanup():
    """后台定期清理过期记录"""
    while True:
        await asyncio.sleep(60)
        try:
            _purge_expired()
        except Exception as e:
            log.warning(f'定期清理异常: {e}')


@on_load
async def _init():
    global _cleanup_task
    _load_targets()
    _cleanup_task = asyncio.create_task(_periodic_cleanup())
    log.info('针对撤回插件已加载')


# ==================== 工具函数 ====================


_appid_cache = None


def _get_appid():
    global _appid_cache
    if _appid_cache:
        return _appid_cache
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref:
            for appid in _bot_manager_ref._bots:
                _appid_cache = appid
                return appid
    except Exception:
        pass
    return ''


def _avatar(uid):
    appid = _get_appid()
    if not appid:
        return ''
    return f'https://q.qlogo.cn/qqapp/{appid}/{uid}/100'


def _is_bot_admin(group_id):
    """检查机器人是否为该群管理员 (从 data.db 查询)"""
    from core.bot.manager import _bot_manager_ref

    if not _bot_manager_ref:
        return False
    bot = next(iter(_bot_manager_ref._bots.values()), None)
    if not bot:
        return False
    rows = bot.log_service.query_data(
        'SELECT group_id FROM group_bot_admin WHERE group_id = ?', (group_id,)
    )
    return bool(rows)


def _is_full_access(event):
    return event.event_type == 'GROUP_MESSAGE_CREATE'


def _is_admin_or_owner(event):
    return event.member_role in ('admin', 'owner')


_DEFAULT_MINUTES = 10
_DURATION_RE = re.compile(r'(?:^e针对\s*)(\d+)', re.IGNORECASE)


def _parse_duration(text):
    """从文本中解析时长(分钟), 返回秒数; 无数字则返回默认 10 分钟"""
    m = _DURATION_RE.search(text)
    if not m:
        return _DEFAULT_MINUTES * 60
    val = int(m.group(1))
    if val == 0:
        return 0
    if val < 0:
        return _DEFAULT_MINUTES * 60
    return val * 60


def _format_remaining(expire):
    """格式化剩余时间"""
    if not expire:
        return '永久'
    remain = expire - time.time()
    if remain <= 0:
        return '已过期'
    if remain >= 86400:
        return f'{remain / 86400:.1f}'
    if remain >= 3600:
        return f'{remain / 3600:.1f}'
    if remain >= 60:
        return f'{remain / 60:.0f}'
    return f'{remain:.0f}'


# ==================== 拦截器: 自动撤回 ====================


@interceptor(priority=10)
async def _auto_recall(event):
    if not event.is_group:
        return
    gid = event.group_id or ''
    uid = event.user_id or ''
    if not gid or not uid:
        return

    group_targets = _targets.get(gid)
    if not group_targets or uid not in group_targets:
        return

    # 检查是否过期
    expire = group_targets[uid]
    if expire and expire <= time.time():
        del group_targets[uid]
        if not group_targets:
            _targets.pop(gid, None)
        _save_targets()
        return

    if not _is_full_access(event):
        return

    mid = event.message_id or ''
    if not mid:
        return

    endpoint = f'/v2/groups/{event.group_openid or gid}/messages/{quote(mid, safe="")}'
    try:
        ok, data = await event.sender.delete(endpoint)
        if ok:
            log.info(f'已撤回被针对用户消息: group={gid} user={uid}')
        else:
            err = data.get('message', '') if isinstance(data, dict) else str(data)
            log.warning(f'撤回失败: group={gid} user={uid} err={err}')
    except Exception as e:
        log.warning(f'撤回异常: group={gid} user={uid} err={e}')

    return True


# ==================== 指令: 针对 ====================


@handler(r'^e针对(?!列表|取消)', name='e针对', desc='e针对 [分钟] @用户 自动撤回其消息 (默认10分钟)', group_only=True, ignore_at_check=True)
async def target_user(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用，<qqbot-cmd-input text="全量申请" show="点击这里授权全量群" />')
        return

    if not _is_admin_or_owner(event):
        await event.reply('仅管理员或群主可使用')
        return

    if not _is_bot_admin(event.group_id):
        await event.reply('机器人不是管理员，请先设置机器人为群管理员\n\n>如果已授权管理员，请艾特机器人发送任意消息验证管理员')
        return

    mentions = event.mentions or []
    user_ids = []
    for m in mentions:
        if not isinstance(m, dict):
            continue
        if m.get('is_you') or m.get('bot'):
            continue
        mid = m.get('member_openid') or m.get('id', '')
        if mid:
            user_ids.append(mid)

    if not user_ids:
        await event.reply('请 @ 需要针对的用户\n\n><qqbot-cmd-input text="e针对 10 " show="针对 时间 @被针对的用户" />\n0为一直针对，默认10分钟')
        return

    content = event.content or ''
    duration = _parse_duration(content)
    expire = 0 if duration == 0 else time.time() + duration

    gid = event.group_id
    if gid not in _targets:
        _targets[gid] = {}

    added = []
    for uid in user_ids:
        if uid not in _targets[gid]:
            _targets[gid][uid] = expire
            added.append(uid)
        else:
            _targets[gid][uid] = expire
            added.append(uid)
    _save_targets()

    if added:
        at_list = ' '.join(f'<@{uid}>' for uid in added)
        dur_text = f' ({_format_remaining(expire)})'
        await event.reply(f'已针对: {at_list}{dur_text}\n发消息将自动撤回')


@handler(r'^e取消针对', name='e取消针对', desc='e取消针对+@用户/openid', group_only=True, ignore_at_check=True)
async def untarget_user(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用，<qqbot-cmd-input text="全量申请" show="点击这里授权全量群" />')
        return

    if not _is_admin_or_owner(event):
        await event.reply('仅管理员或群主可使用')
        return

    # 从 @mention 中提取
    mentions = event.mentions or []
    user_ids = []
    for m in mentions:
        if not isinstance(m, dict):
            continue
        if m.get('is_you') or m.get('bot'):
            continue
        mid = m.get('member_openid') or m.get('id', '')
        if mid:
            user_ids.append(mid)

    # 从文本中提取 openid (支持直接输入 openid)
    content = event.content or ''
    text_after_cmd = re.sub(r'^e取消针对\s*', '', content).strip()
    # 移除 @mention 文本, 剩余部分按空格分割作为 openid
    for m in mentions:
        if isinstance(m, dict):
            name = m.get('username', '')
            if name:
                text_after_cmd = text_after_cmd.replace(f'@{name}', '').strip()
    if text_after_cmd:
        for token in text_after_cmd.split():
            token = token.strip()
            if len(token) >= 8 and token not in user_ids:
                user_ids.append(token)

    if not user_ids:
        await event.reply('请 @ 需要取消针对的用户，或输入 openid')
        return

    gid = event.group_id
    group_targets = _targets.get(gid, {})

    removed = []
    for uid in user_ids:
        if uid in group_targets:
            del group_targets[uid]
            removed.append(uid)

    if not group_targets:
        _targets.pop(gid, None)
    _save_targets()

    if removed:
        at_list = ' '.join(f'<@{uid}>' for uid in removed)
        await event.reply(f'已取消针对: {at_list}')
    else:
        await event.reply('这些用户不在针对列表中')


@handler(r'^e针对列表$', name='e针对列表', desc='查看当前群针对列表', group_only=True, ignore_at_check=True)
async def list_targets(event, match):
    if not _is_admin_or_owner(event):
        await event.reply('仅管理员或群主可查看')
        return

    gid = event.group_id
    _purge_expired()
    group_targets = _targets.get(gid, {})
    if not group_targets:
        await event.reply('当前无针对用户')
        return

    lines = []
    for uid, expire in group_targets.items():
        remain = _format_remaining(expire)
        cancel_btn = f'<qqbot-cmd-input text="e取消针对 {uid}" show="取消" />'
        av = _avatar(uid)
        avatar_md = f'![头像 #20px #20px]({av}) ' if av else ''
        lines.append(f'{avatar_md}<@{uid}> ({remain}) {cancel_btn}')
    await event.reply('针对列表:\n' + '\n'.join(lines))


