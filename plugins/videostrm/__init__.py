import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote, unquote
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.utils.http import RequestUtils
from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger

# 尝试导入Playwright
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.info("Playwright未安装，VideoStrm插件功能受限")


class VideoStrm(_PluginBase):
    # 插件名称
    plugin_name = "VideoStrm"
    # 插件描述
    plugin_desc = "搜索视频并自动生成STRM文件，支持m3u8流媒体"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/cyDione/MoviePilot-Plugins/main/icons/videostrm.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "cydione"
    # 作者主页
    author_url = "https://github.com/cyDione"
    # 插件配置项ID前缀
    plugin_config_prefix = "videostrm_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _cron = None
    _onlyonce = False
    _storageplace = None
    _search_keywords = None
    _base_url = "https://missav.live"

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._storageplace = config.get("storageplace")
            self._search_keywords = config.get("search_keywords")

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._enabled and self._cron:
                try:
                    self._scheduler.add_job(
                        func=self.__task,
                        trigger=CronTrigger.from_crontab(self._cron),
                        name="VideoStrm文件创建"
                    )
                    logger.info(f'VideoStrm定时任务创建成功：{self._cron}')
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            if self._onlyonce:
                logger.info("VideoStrm服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.__task,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="VideoStrm文件创建"
                )
                # 关闭一次性开关
                self._onlyonce = False

            self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def search_videos(self, keyword: str) -> List[Dict[str, str]]:
        """
        搜索视频，返回视频列表
        :param keyword: 搜索关键词
        :return: [{'title': '标题', 'url': '视频页面URL', 'code': '番号'}]
        """
        if not PLAYWRIGHT_AVAILABLE:
            logger.warn("Playwright不可用，无法搜索视频")
            return []

        search_url = f"{self._base_url}/en/search/{quote(keyword)}"
        results = []

        try:
            logger.info(f"正在搜索: {keyword}")

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                try:
                    page.goto(search_url, wait_until='networkidle', timeout=30000)
                    # 等待页面加载
                    page.wait_for_timeout(5000)

                    # 获取视频卡片
                    video_cards = page.query_selector_all('a[href*="/en/"]')

                    for card in video_cards:
                        href = card.get_attribute('href')
                        if href and re.search(r'/[a-zA-Z0-9]+-\d+', href):
                            # 提取番号
                            code_match = re.search(r'/([a-zA-Z0-9]+-\d+)', href)
                            if code_match:
                                code = code_match.group(1).upper()
                                # 获取标题
                                title_elem = card.query_selector('h2, .title, [class*="title"]')
                                title = title_elem.text_content().strip() if title_elem else code

                                full_url = href if href.startswith('http') else f"{self._base_url}{href}"

                                # 避免重复
                                if not any(r['code'] == code for r in results):
                                    results.append({
                                        'title': title,
                                        'url': full_url,
                                        'code': code
                                    })

                    logger.info(f"搜索 '{keyword}' 找到 {len(results)} 个结果")

                finally:
                    browser.close()

        except Exception as e:
            logger.error(f"搜索视频失败: {e}")

        return results

    def get_video_source(self, video_url: str) -> Optional[str]:
        """
        从视频页面提取m3u8链接
        :param video_url: 视频页面URL
        :return: m3u8流媒体地址
        """
        if not PLAYWRIGHT_AVAILABLE:
            logger.warn("Playwright不可用，无法提取视频源")
            return None

        try:
            logger.info(f"正在提取视频源: {video_url}")

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                try:
                    page.goto(video_url, wait_until='networkidle', timeout=30000)
                    page.wait_for_timeout(5000)

                    # 获取页面内容
                    content = page.content()

                    # 查找m3u8链接
                    m3u8_patterns = [
                        r'https?://[^\s"\'`]+\.m3u8[^\s"\'`]*',
                        r'"(https?://surrit\.com/[^"]+\.m3u8[^"]*)"',
                        r"'(https?://surrit\.com/[^']+\.m3u8[^']*)'",
                    ]

                    for pattern in m3u8_patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            # 返回第一个有效的m3u8链接
                            m3u8_url = matches[0]
                            # 清理URL
                            m3u8_url = m3u8_url.strip('"\'')
                            logger.info(f"找到视频源: {m3u8_url[:50]}...")
                            return m3u8_url

                    # 如果没找到，尝试从script标签中查找
                    scripts = page.query_selector_all('script')
                    for script in scripts:
                        script_content = script.text_content() or ""
                        matches = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', script_content)
                        if matches:
                            return matches[0]

                    logger.warn(f"未找到m3u8链接: {video_url}")
                    return None

                finally:
                    browser.close()

        except Exception as e:
            logger.error(f"提取视频源失败: {e}")
            return None

    def create_strm_file(self, title: str, source_url: str, code: str = None) -> bool:
        """
        创建STRM文件
        :param title: 视频标题
        :param source_url: 视频源地址
        :param code: 番号（可选，用于文件名）
        :return: 是否成功创建
        """
        if not self._storageplace:
            logger.error("未配置STRM存储路径")
            return False

        # 确保存储目录存在
        os.makedirs(self._storageplace, exist_ok=True)

        # 生成文件名（使用番号或标题）
        filename = code if code else self._sanitize_filename(title)
        file_path = os.path.join(self._storageplace, f"{filename}.strm")

        if os.path.exists(file_path):
            logger.debug(f'{filename}.strm 文件已存在')
            return False

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(source_url)
            logger.info(f'创建 {filename}.strm 成功')
            return True
        except Exception as e:
            logger.error(f'创建STRM文件失败: {e}')
            return False

    def _sanitize_filename(self, filename: str) -> str:
        """清理文件名，移除非法字符"""
        # 移除非法字符
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # 移除多余空白
        filename = ' '.join(filename.split())
        # 限制长度
        return filename[:200] if len(filename) > 200 else filename

    def __task(self):
        """执行定时任务"""
        if not self._search_keywords:
            logger.warn("未配置搜索关键词")
            return

        # 解析关键词列表
        keywords = [k.strip() for k in self._search_keywords.split('\n') if k.strip()]

        if not keywords:
            logger.warn("搜索关键词为空")
            return

        total_created = 0

        for keyword in keywords:
            logger.info(f"开始处理关键词: {keyword}")

            # 搜索视频
            videos = self.search_videos(keyword)

            for video in videos:
                try:
                    # 提取视频源
                    source_url = self.get_video_source(video['url'])

                    if source_url:
                        # 创建STRM文件
                        if self.create_strm_file(
                            title=video['title'],
                            source_url=source_url,
                            code=video.get('code')
                        ):
                            total_created += 1

                    # 避免请求过于频繁
                    time.sleep(2)

                except Exception as e:
                    logger.error(f"处理视频失败 {video.get('code', video['title'])}: {e}")

        logger.info(f"任务完成，共创建 {total_created} 个STRM文件")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 8 * * *'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'storageplace',
                                            'label': 'STRM存储路径',
                                            'placeholder': '/downloads/strm'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'search_keywords',
                                            'label': '搜索关键词',
                                            'placeholder': '每行一个关键词，例如：\nABP-\nSSNI-\n明日花绮罗',
                                            'rows': 5
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '使用说明：\n'
                                                    '1. 配置搜索关键词（番号前缀、演员名等），每行一个\n'
                                                    '2. 设置STRM文件存储路径\n'
                                                    '3. 配置定时任务或点击"立即运行一次"\n'
                                                    '4. 生成的STRM文件可通过目录监控转移到媒体库',
                                            'style': 'white-space: pre-line;'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '注意：本插件需要Playwright支持。\n'
                                                    '如果Playwright未安装，插件功能将不可用。',
                                            'style': 'white-space: pre-line;'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "storageplace": "/downloads/strm",
            "cron": "0 8 * * *",
            "search_keywords": "",
        }

    def __update_config(self):
        """更新配置"""
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "storageplace": self._storageplace,
            "search_keywords": self._search_keywords,
        })

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")
