import re


def get_client_ip(request):
    xff = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    if xff:
        return xff
    return request.META.get('REMOTE_ADDR', '') or ''


def parse_user_agent_label(user_agent: str, client_hints_model: str = ''):
    ua = (user_agent or '').lower()
    model = (client_hints_model or '').strip()
    browser = 'Unknown Browser'
    if 'edg/' in ua:
        browser = 'Edge'
    elif 'opr/' in ua or 'opera' in ua:
        browser = 'Opera'
    elif 'chrome/' in ua and 'edg/' not in ua:
        browser = 'Chrome'
    elif 'safari/' in ua and 'chrome/' not in ua:
        browser = 'Safari'
    elif 'firefox/' in ua:
        browser = 'Firefox'
    elif 'msie' in ua or 'trident/' in ua:
        browser = 'IE'

    os_name = 'Unknown OS'
    if 'android' in ua:
        os_name = 'Android'
    elif 'iphone' in ua or 'ipad' in ua or 'ios' in ua:
        os_name = 'iOS'
    elif 'windows' in ua:
        os_name = 'Windows'
    elif 'mac os' in ua or 'macintosh' in ua:
        os_name = 'macOS'
    elif 'linux' in ua:
        os_name = 'Linux'

    if model:
        return f"{browser} on {os_name}, {model}"
    return f"{browser} on {os_name}"
