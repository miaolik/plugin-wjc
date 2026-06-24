"""违禁词撤回插件 — 命中违禁词自动撤回, 支持全局/分群词库, 开关, Web 后台配置"""

import json
import os
import re
from urllib.parse import quote

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, interceptor, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

log = get_logger(PLUGIN, '违禁词')

__plugin_meta__ = {
    'name': '违禁词',
    'author': 'ElainaBot',
    'description': '命中违禁词自动撤回, 支持全局/分群词库与 Web 后台配置',
    'version': '1.0.0',
}

# ==================== 数据持久化 ====================

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_PLUGIN_DIR, 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
_DATA_FILE = os.path.join(_DATA_DIR, 'banned_words.json')

# 默认超级管理员 (可在 Web 后台修改); 可管理全局违禁词与超管列表
_DEFAULT_SUPER_ADMINS = ['538389445D765D2988BFE31506C54799']

# 数据结构:
# {
#   "global": ["词1", ...],              # 全局违禁词
#   "groups": { "群号": ["词", ...] },   # 分群违禁词
#   "global_enabled": true/false,        # 全局违禁词总开关 (对所有群生效)
#   "enabled": { "群号": true/false },   # 分群开关 (缺省视为关闭)
#   "super_admins": ["openid", ...]      # 超级管理员
# }
_data: dict = {}


def _default_data() -> dict:
    return {
        'global': [],
        'groups': {},
        'global_enabled': False,
        'enabled': {},
        'super_admins': list(_DEFAULT_SUPER_ADMINS),
    }


def _normalize(raw) -> dict:
    d = _default_data()
    if not isinstance(raw, dict):
        return d
    if isinstance(raw.get('global'), list):
        d['global'] = [str(w) for w in raw['global'] if str(w).strip()]
    if isinstance(raw.get('groups'), dict):
        for gid, words in raw['groups'].items():
            if isinstance(words, list):
                d['groups'][str(gid)] = [str(w) for w in words if str(w).strip()]
    if 'global_enabled' in raw:
        d['global_enabled'] = bool(raw.get('global_enabled'))
    if isinstance(raw.get('enabled'), dict):
        for gid, val in raw['enabled'].items():
            d['enabled'][str(gid)] = bool(val)
    if isinstance(raw.get('super_admins'), list) and raw['super_admins']:
        d['super_admins'] = [str(a) for a in raw['super_admins'] if str(a).strip()]
    return d


def _load():
    global _data
    if not os.path.isfile(_DATA_FILE):
        _data = _default_data()
        _save()
        return
    try:
        with open(_DATA_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        raw = None
    _data = _normalize(raw)


def _save():
    with open(_DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(_data, f, ensure_ascii=False, indent=2)


# ==================== 工具函数 ====================


def _is_admin_or_owner(event) -> bool:
    return getattr(event, 'member_role', '') in ('admin', 'owner')


def _is_super_admin(event) -> bool:
    return (event.user_id or '') in _data.get('super_admins', [])


def _is_full_access(event) -> bool:
    return getattr(event, 'event_type', '') == 'GROUP_MESSAGE_CREATE'


def _group_enabled(gid: str) -> bool:
    return bool(_data.get('enabled', {}).get(str(gid)))


def _global_enabled() -> bool:
    return bool(_data.get('global_enabled'))


# 管理指令前缀: 这些消息不参与自动撤回 (否则删词指令会被自己拦下)
_MGMT_PREFIXES = (
    '违禁词全局开启', '违禁词全局关闭',
    '违禁词开启', '违禁词关闭', '违禁词列表',
    '新增全局违禁词', '删除全局违禁词',
    '新增违禁词', '删除违禁词',
)


def _is_mgmt_command(content: str) -> bool:
    c = (content or '').strip()
    return any(c.startswith(p) for p in _MGMT_PREFIXES)


def _match_word(content: str, gid: str):
    """返回命中的第一个违禁词, 否则 None (子串包含匹配)

    全局词受全局开关控制, 本群词受本群开关控制, 两者独立。"""
    if not content:
        return None
    words = []
    if _global_enabled():
        words.extend(_data.get('global', []))
    if _group_enabled(gid):
        words.extend(_data.get('groups', {}).get(str(gid), []))
    for w in words:
        if w and w in content:
            return w
    return None


def _strip_cmd(content: str, prefix_re: str) -> str:
    """去掉指令前缀和 @mention 文本, 返回违禁词正文"""
    text = re.sub(prefix_re, '', content or '', count=1)
    text = re.sub(r'<@!?[^>]+>', '', text)
    return text.strip()


# ==================== 拦截器: 自动撤回 ====================


@interceptor(priority=10)
async def _auto_recall(event):
    if not getattr(event, 'is_group', False):
        return
    gid = event.group_id or ''
    if not gid:
        return
    if not (_global_enabled() or _group_enabled(gid)):
        return

    content = event.content or ''
    # 管理指令本身不被撤回 (否则 “删除违禁词 广告” 这类指令会被自己命中拦下)
    if _is_mgmt_command(content):
        return

    word = _match_word(content, gid)
    if not word:
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
            log.info(f'已撤回违禁词消息: group={gid} user={event.user_id} word={word}')
        else:
            err = data.get('message', '') if isinstance(data, dict) else str(data)
            log.warning(f'撤回失败: group={gid} word={word} err={err}')
    except Exception as e:
        log.warning(f'撤回异常: group={gid} word={word} err={e}')

    return True


# ==================== 指令: 开关 ====================


@handler(r'^违禁词开启$', name='违禁词开启', desc='开启本群违禁词撤回', group_only=True, ignore_at_check=True)
async def enable_group(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用，<qqbot-cmd-input text="全量申请" show="点击这里授权全量群" />')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    _data.setdefault('enabled', {})[str(event.group_id)] = True
    _save()
    await event.reply('✅ 已开启本群违禁词撤回')


@handler(r'^违禁词关闭$', name='违禁词关闭', desc='关闭本群违禁词撤回', group_only=True, ignore_at_check=True)
async def disable_group(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用，<qqbot-cmd-input text="全量申请" show="点击这里授权全量群" />')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    _data.setdefault('enabled', {})[str(event.group_id)] = False
    _save()
    await event.reply('🛑 已关闭本群违禁词撤回')


@handler(r'^违禁词全局开启$', name='违禁词全局开启', desc='开启全局违禁词 (对所有群生效, 超管)', ignore_at_check=True)
async def enable_global(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局开关')
        return
    _data['global_enabled'] = True
    _save()
    await event.reply('✅ 已开启全局违禁词 (对所有群生效)')


@handler(r'^违禁词全局关闭$', name='违禁词全局关闭', desc='关闭全局违禁词 (超管)', ignore_at_check=True)
async def disable_global(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局开关')
        return
    _data['global_enabled'] = False
    _save()
    await event.reply('🛑 已关闭全局违禁词')


# ==================== 指令: 分群违禁词增删 ====================


@handler(r'^新增违禁词', name='新增违禁词', desc='新增违禁词 词1 词2 ... (本群)', group_only=True, ignore_at_check=True)
async def add_group_word(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用，<qqbot-cmd-input text="全量申请" show="点击这里授权全量群" />')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    words = _strip_cmd(event.content, r'^新增违禁词\s*').split()
    if not words:
        await event.reply('用法: 新增违禁词 词1 词2 ...')
        return
    gid = str(event.group_id)
    lst = _data.setdefault('groups', {}).setdefault(gid, [])
    added = []
    for w in words:
        if w not in lst:
            lst.append(w)
            added.append(w)
    _save()
    if added:
        await event.reply(f'✅ 已添加本群违禁词: {" ".join(added)}\n共 {len(lst)} 个')
    else:
        await event.reply('这些词已存在')


@handler(r'^删除违禁词', name='删除违禁词', desc='删除违禁词 词1 词2 ... (本群)', group_only=True, ignore_at_check=True)
async def del_group_word(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用，<qqbot-cmd-input text="全量申请" show="点击这里授权全量群" />')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    words = _strip_cmd(event.content, r'^删除违禁词\s*').split()
    if not words:
        await event.reply('用法: 删除违禁词 词1 词2 ...')
        return
    gid = str(event.group_id)
    lst = _data.get('groups', {}).get(gid, [])
    removed = [w for w in words if w in lst]
    for w in removed:
        lst.remove(w)
    if gid in _data.get('groups', {}) and not _data['groups'][gid]:
        _data['groups'].pop(gid, None)
    _save()
    if removed:
        await event.reply(f'✅ 已删除本群违禁词: {" ".join(removed)}')
    else:
        await event.reply('这些词不在本群词库中')


# ==================== 指令: 全局违禁词增删 (超管) ====================


@handler(r'^新增全局违禁词', name='新增全局违禁词', desc='新增全局违禁词 词1 词2 ... (超管)', ignore_at_check=True)
async def add_global_word(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局违禁词')
        return
    words = _strip_cmd(event.content, r'^新增全局违禁词\s*').split()
    if not words:
        await event.reply('用法: 新增全局违禁词 词1 词2 ...')
        return
    lst = _data.setdefault('global', [])
    added = []
    for w in words:
        if w not in lst:
            lst.append(w)
            added.append(w)
    _save()
    if added:
        await event.reply(f'✅ 已添加全局违禁词: {" ".join(added)}\n共 {len(lst)} 个')
    else:
        await event.reply('这些词已存在')


@handler(r'^删除全局违禁词', name='删除全局违禁词', desc='删除全局违禁词 词1 词2 ... (超管)', ignore_at_check=True)
async def del_global_word(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局违禁词')
        return
    words = _strip_cmd(event.content, r'^删除全局违禁词\s*').split()
    if not words:
        await event.reply('用法: 删除全局违禁词 词1 词2 ...')
        return
    lst = _data.get('global', [])
    removed = [w for w in words if w in lst]
    for w in removed:
        lst.remove(w)
    _save()
    if removed:
        await event.reply(f'✅ 已删除全局违禁词: {" ".join(removed)}')
    else:
        await event.reply('这些词不在全局词库中')


# ==================== 指令: 列表 ====================


@handler(r'^违禁词列表$', name='违禁词列表', desc='查看全局与本群违禁词', group_only=True, ignore_at_check=True)
async def list_words(event, match):
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可查看')
        return
    gid = str(event.group_id)
    g = _data.get('global', [])
    grp = _data.get('groups', {}).get(gid, [])
    grp_status = '开启' if _group_enabled(gid) else '关闭'
    glb_status = '开启' if _global_enabled() else '关闭'
    lines = [f'本群开关: {grp_status}    全局开关: {glb_status}']
    lines.append(f'\n全局违禁词({len(g)}): ' + ('、'.join(g) if g else '无'))
    lines.append(f'本群违禁词({len(grp)}): ' + ('、'.join(grp) if grp else '无'))
    await event.reply('\n'.join(lines))


# ==================== Web 后台配置 ====================

_PAGE_KEY = 'banned-words'


def _json_resp(obj, status=200):
    from aiohttp import web
    return web.json_response(obj, status=status)


@register_route('GET', '/api/ext/banned/config')
async def _web_get_config(request):
    return _json_resp({
        'global': _data.get('global', []),
        'groups': _data.get('groups', {}),
        'global_enabled': _data.get('global_enabled', False),
        'enabled': _data.get('enabled', {}),
        'super_admins': _data.get('super_admins', []),
    })


@register_route('POST', '/api/ext/banned/config')
async def _web_set_config(request):
    try:
        body = await request.json()
    except Exception:
        return _json_resp({'ok': False, 'msg': '请求体不是合法 JSON'}, status=400)
    global _data
    _data = _normalize(body)
    _save()
    return _json_resp({'ok': True})


@on_load
async def _init():
    _load()
    register_page(
        key=_PAGE_KEY,
        label='违禁词配置',
        source='plugin',
        source_name='违禁词',
        html_file=os.path.join(_PLUGIN_DIR, 'page.html'),
        icon='shield',
    )
    log.info('违禁词插件已加载')


@on_unload
def _cleanup():
    unregister_page(_PAGE_KEY)
