import os
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

# ======================== 配置 ========================
script_dir = os.path.dirname(os.path.abspath(__file__))
NEW_M3U_PATH = os.path.join(script_dir, 'new.m3u')

# 延迟阈值（秒），超过此值的链接会被过滤
MAX_LATENCY_THRESHOLD = 10.0

# 读取播放列表内容的最大字节数（1MB 足以容纳绝大多数 M3U）
MAX_PLAYLIST_SIZE = 1024 * 1024

# 创建全局 Session，复用 TCP 连接
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'keep-alive',
})
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
session.mount('http://', adapter)
session.mount('https://', adapter)


# ======================== 辅助函数 ========================
def parse_extvlcopt_headers(extvlcopt_line):
    """
    解析 EXTVLCOPT 行，提取 HTTP 请求头
    """
    headers = {}
    if not extvlcopt_line:
        return headers

    line_content = extvlcopt_line.replace('#EXTVLCOPT:', '').strip()
    options = line_content.split()

    for option in options:
        if '=' in option:
            key, value = option.split('=', 1)
            if key == 'http-user-agent':
                headers['User-Agent'] = value
            elif key in ('http-referrer', 'http-referer'):
                headers['Referer'] = value
            elif key == 'http-cookie':
                headers['Cookie'] = value
            elif key.startswith('http-'):
                header_key = key.replace('http-', '').replace('-', ' ').title().replace(' ', '-')
                headers[header_key] = value
    return headers


def validate_playlist_and_slice(playlist_url, headers, extvlcopt_line=None, depth=0):
    """
    递归验证播放列表及其第一个切片/子流是否可达。
    返回 (是否有效, 最终播放列表URL, 响应时间秒数)
    """
    if depth > 3:   # 防止无限递归
        return False, None, 0

    try:
        # 合并请求头
        custom_headers = parse_extvlcopt_headers(extvlcopt_line) if extvlcopt_line else {}
        req_headers = session.headers.copy()
        req_headers.update(custom_headers)
        req_headers.update(headers)   # 外部传入的 headers 具有最高优先级

        start_time = time.time()
        resp = session.get(
            playlist_url,
            stream=True,
            timeout=15,
            allow_redirects=True,
            headers=req_headers
        )
        elapsed = time.time() - start_time

        if resp.status_code != 200:
            resp.close()
            return False, resp.url, elapsed

        # 读取完整内容（限制大小）
        content_bytes = b''
        for chunk in resp.iter_content(chunk_size=4096):
            content_bytes += chunk
            if len(content_bytes) >= MAX_PLAYLIST_SIZE:
                break
        resp.close()

        # 解码为文本
        try:
            content = content_bytes.decode('utf-8', errors='ignore')
        except:
            return False, resp.url, elapsed

        # 基本格式校验
        if not ('#EXTM3U' in content or '#EXTINF' in content):
            return False, resp.url, elapsed

        final_base_url = resp.url   # 最终重定向后的地址，用作 base

        # 提取第一个可用的子列表或切片 URL
        lines = content.splitlines()
        target_url = None
        is_sub_playlist = False

        for i, line in enumerate(lines):
            line = line.strip()
            # 优先检测 #EXT-X-STREAM-INF（主列表）
            if '#EXT-X-STREAM-INF' in line and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line and not next_line.startswith('#'):
                    target_url = urljoin(final_base_url, next_line)
                    is_sub_playlist = True
                    break
            # 检测 #EXTINF（媒体列表）
            elif line.startswith('#EXTINF:') and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line and not next_line.startswith('#'):
                    target_url = urljoin(final_base_url, next_line)
                    break
        else:
            # 没有找到任何切片/子流
            return False, final_base_url, elapsed

        # 如果找到的是子播放列表，递归验证
        if is_sub_playlist:
            return validate_playlist_and_slice(target_url, headers, extvlcopt_line, depth + 1)

        # 否则，测试切片是否可达（HEAD 请求，携带相同请求头）
        try:
            head_resp = session.head(target_url, headers=req_headers, timeout=5)
            if head_resp.status_code in (200, 301, 302, 303, 307):
                return True, final_base_url, elapsed
            # 有些服务器不支持 HEAD，尝试 GET 少量数据
            get_resp = session.get(target_url, headers=req_headers, timeout=5, stream=True)
            get_resp.close()
            if get_resp.status_code == 200:
                return True, final_base_url, elapsed
        except:
            pass

        return False, final_base_url, elapsed

    except Exception as e:
        return False, playlist_url, 0


def test_link_optimized(url, extvlcopt_line=None):
    """
    优化版链接测试：验证播放列表及其内部切片/子流是否有效。
    返回 (是否有效, 最终播放列表URL, 响应时间秒数)
    """
    return validate_playlist_and_slice(url, {}, extvlcopt_line)


# ======================== 页面处理 ========================
def process_page(page_url):
    """
    处理单个 M3U 页面（不写磁盘，内存中处理）
    返回该页面中有效的链接列表，每个元素为 (extinf, extvlcopt, final_url, latency)
    """
    try:
        resp = session.get(page_url, timeout=15)
        resp.raise_for_status()
        lines = resp.text.splitlines()
    except Exception as e:
        print(f"下载失败 {page_url}: {e}")
        return []

    links_to_test = []
    current_group_title = ''
    extinf = ''
    extvlcopt = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 处理 EXTINF
        if line.startswith("#EXTINF"):
            extinf = line
            match = re.search(r'group-title="([^"]*)"', line)
            if match:
                current_group_title = match.group(1)
            else:
                current_group_title = ''

            # 分组过滤
            if current_group_title in ['Information']:
                print(f'已过滤分组: {current_group_title}')
                current_group_title = ''   # 标记跳过
            continue

        # 跳过被过滤分组下的所有链接
        if current_group_title == '':
            continue

        # 处理 EXTVLCOPT
        if line.startswith("#EXTVLCOPT:"):
            extvlcopt = line
            continue

        # 处理链接行
        if line.startswith(("http://", "https://", "rtmp://")):
            link = line
            # 域名黑名单过滤
            domain_match = re.search(r'://([^/]+)/', link)
            if domain_match:
                domain = domain_match.group(1)
                if domain in ["sc2022.stream-link.org", "39.134.24.162", "epg.pw"]:
                    print(f"域名黑名单，跳过: {link}")
                    extvlcopt = None
                    continue

            links_to_test.append((extinf, extvlcopt, link))
            extvlcopt = None   # 重置

    if not links_to_test:
        print(f"{page_url} 没有需要测试的链接")
        return []

    print(f"{page_url} 共有 {len(links_to_test)} 个链接待测试，开始并发检测...")

    max_workers = min(10, max(2, len(links_to_test) // 20 + 1))
    valid_results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {
            executor.submit(test_link_optimized, link[-1], link[1]): link
            for link in links_to_test
        }
        for future in as_completed(future_to_item):
            extinf, extvlcopt, link = future_to_item[future]
            try:
                is_valid, final_url, latency = future.result()
                if is_valid:
                    valid_results.append((extinf, extvlcopt, final_url, latency))
                    print(f"✓ 有效: {link} ({latency:.2f}s)")
                else:
                    print(f"✗ 无效: {link}")
            except Exception as e:
                print(f"测试异常 {link}: {e}")

    print(f"{page_url} 处理完成，有效链接数: {len(valid_results)}")
    return valid_results


# ======================== 主程序 ========================
def main():
    pages = [
        'https://iptv-org.github.io/iptv/index.country.m3u',
        'https://raw.githubusercontent.com/luongz/Japan-IPTV/main/jp.m3u',
        'https://raw.githubusercontent.com/akkradet/IPTV-THAI/refs/heads/master/FREETV.m3u',
    ]

    all_valid = []
    for page in pages:
        print(f"\n=== 处理页面: {page} ===")
        results = process_page(page)
        all_valid.extend(results)

    print(f"\n总共获得 {len(all_valid)} 条有效链接，正在写入 {NEW_M3U_PATH} ...")
    with open(NEW_M3U_PATH, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        for extinf, extvlcopt, url, latency in all_valid:
            f.write(extinf + '\n')
            if extvlcopt:
                f.write(extvlcopt + '\n')
            f.write(url + '\n')
    print(f"完成！有效链接已保存至 {NEW_M3U_PATH}")


if __name__ == "__main__":
    main()
