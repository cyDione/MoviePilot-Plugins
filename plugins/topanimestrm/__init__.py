import os
import time
from datetime import datetime, timedelta
from urllib.parse import quote, unquote
import re

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.utils.http import RequestUtils
from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
import xml.dom.minidom
from app.utils.dom import DomUtils

# 尝试导入Playwright，如果失败则使用备用方案
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.info("Playwright未安装，将使用备用方案")


def retry(ExceptionToCheck: Any,
          tries: int = 3, delay: int = 3, backoff: int = 1, logger: Any = None, ret: Any = None):
    """
    :param ExceptionToCheck: 需要捕获的异常
    :param tries: 重试次数
    :param delay: 延迟时间
    :param backoff: 延迟倍数
    :param logger: 日志对象
    :param ret: 默认返回
    """

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 0:
                try:
                    return f(*args, **kwargs)
                except ExceptionToCheck as e:
                    msg = f"未获取到文件信息，{mdelay}秒后重试 ..."
                    if logger:
                        logger.warn(msg)
                    else:
                        print(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            if logger:
                logger.warn('请确保当前季度番剧文件夹存在或检查网络问题')
            return ret

        return f_retry

    return deco_retry


class TopAnimeStrm(_PluginBase):
    # 插件名称
    plugin_name = "TopAnimeStrm"
    # 插件描述
    plugin_desc = "自动获取当季TOP15番剧的全集，免去下载，轻松拥有一个番剧媒体库"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/cyDione/MoviePilot-Plugins/main/icons/anistrm.png"
    # 插件版本
    plugin_version = "2.6.0"
    # 插件作者
    plugin_author = "cydione"
    # 作者主页
    author_url = "https://github.com/cyDione"
    # 插件配置项ID前缀
    plugin_config_prefix = "topanimestrm_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _onlyonce = False
    _fulladd = False
    _storageplace = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._fulladd = config.get("fulladd")
            self._storageplace = config.get("storageplace")
            # 加载模块
        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._enabled and self._cron:
                try:
                    self._scheduler.add_job(func=self.__task,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="TopAnimeStrm文件创建")
                    logger.info(f'TopAnimeStrm定时任务创建成功：{self._cron}')
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            if self._onlyonce:
                logger.info(f"TopAnimeStrm服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__task, args=[self._fulladd], trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="TopAnimeStrm文件创建")
                # 关闭一次性开关 全量转移
                self._onlyonce = False
                self._fulladd = False
            self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __get_ani_season(self, idx_month: int = None) -> str:
        current_date = datetime.now()
        current_year = current_date.year
        current_month = idx_month if idx_month else current_date.month
        for month in range(current_month, 0, -1):
            if month in [10, 7, 4, 1]:
                self._date = f'{current_year}-{month}'
                return f'{current_year}-{month}'

    @retry(Exception, tries=3, logger=logger, ret=[])
    def get_current_season_list(self) -> List:
        """获取当前季度TOP15番剧的所有集数"""
        if not PLAYWRIGHT_AVAILABLE:
            logger.warn("Playwright不可用，跳过季度API")
            return []
            
        season = self.__get_ani_season()
        url = f'https://openani.an-i.workers.dev/{season}/'
        
        try:
            logger.info(f"正在使用Playwright获取季度页面: {url}")
            
            with sync_playwright() as p:
                # 启动浏览器
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                try:
                    # 访问页面
                    page.goto(url, wait_until='networkidle')
                    
                    # 等待页面完全加载
                    page.wait_for_timeout(5000)  # 等待5秒
                    
                    # 尝试等待视频元素出现
                    try:
                        page.wait_for_selector('video, .file, [data-filename]', timeout=10000)
                    except:
                        logger.warn("未找到预期的视频元素，继续尝试解析...")
                    
                    # 获取页面内容
                    content = page.content()
                    
                    # 查找视频文件
                    video_files = []
                    
                    # 方法1: 查找video标签
                    video_elements = page.query_selector_all('video')
                    for video in video_elements:
                        src = video.get_attribute('src')
                        if src and src.endswith('.mp4'):
                            video_files.append(src.split('/')[-1])
                    
                    # 方法2: 查找包含.mp4的链接
                    links = page.query_selector_all('a[href*=".mp4"]')
                    for link in links:
                        href = link.get_attribute('href')
                        if href and '.mp4' in href:
                            filename = href.split('/')[-1]
                            if filename not in video_files:
                                video_files.append(filename)
                    
                    # 方法3: 查找data属性中的文件名
                    elements_with_data = page.query_selector_all('[data-filename]')
                    for element in elements_with_data:
                        filename = element.get_attribute('data-filename')
                        if filename and '.mp4' in filename and filename not in video_files:
                            video_files.append(filename)
                    
                    # 方法4: 在页面源码中查找文件名
                    patterns = [
                        r'"([^"]*\.mp4[^"]*)"',
                        r"'([^']*\.mp4[^']*)'",
                        r'filename["\']?\s*:\s*["\']([^"\']*\.mp4[^"\']*)["\']',
                        r'name["\']?\s*:\s*["\']([^"\']*\.mp4[^"\']*)["\']',
                        r'src["\']?\s*:\s*["\']([^"\']*\.mp4[^"\']*)["\']',
                        r'href["\']?\s*:\s*["\']([^"\']*\.mp4[^"\']*)["\']',
                    ]
                    
                    for pattern in patterns:
                        matches = re.findall(pattern, content)
                        for match in matches:
                            if match and match not in video_files:
                                video_files.append(match)
                    
                    # 方法5: 查找所有包含.mp4的文本内容
                    all_text = page.text_content('body')
                    mp4_matches = re.findall(r'([^/\s]+\.mp4[^\s]*)', all_text)
                    for match in mp4_matches:
                        if match not in video_files:
                            video_files.append(match)
                    
                    logger.info(f"从Playwright渲染的页面中解析到 {len(video_files)} 个视频文件")
                    
                    # 清理文件名并获取TOP15
                    cleaned_files = self._clean_and_get_top15(video_files)
                    
                    return cleaned_files
                    
                finally:
                    browser.close()
                
        except Exception as e:
            logger.error(f"Playwright获取季度番剧列表失败: {e}")
            return []

    def _clean_and_get_top15(self, video_files: List[str]) -> List[str]:
        """清理文件名并获取TOP15番剧的所有集数"""
        cleaned_files = []
        anime_series = {}  # 用于统计每个番剧的集数
        
        for filename in video_files:
            # 清理文件名
            clean_name = self._clean_filename(filename)
            if not clean_name:
                continue
                
            # 提取番剧名称（去掉集数）
            anime_name = self._extract_anime_name(clean_name)
            if anime_name:
                if anime_name not in anime_series:
                    anime_series[anime_name] = []
                anime_series[anime_name].append(clean_name)
        
        # 按集数排序，选择集数最多的TOP15番剧
        sorted_anime = sorted(anime_series.items(), key=lambda x: len(x[1]), reverse=True)
        top15_anime = sorted_anime[:15]
        
        logger.info(f"找到 {len(anime_series)} 个不同的番剧")
        logger.info("TOP15番剧及其所有集数:")
        for i, (anime_name, episodes) in enumerate(top15_anime, 1):
            # 按集数排序
            sorted_episodes = sorted(episodes, key=lambda x: self._extract_episode_number(x))
            logger.info(f"  {i}. {anime_name} ({len(episodes)}集)")
            # 添加该番剧的所有集数
            cleaned_files.extend(sorted_episodes)
        
        return cleaned_files

    def _clean_filename(self, filename: str) -> str:
        """清理文件名，去掉URL编码和?a=view部分"""
        if not filename:
            return ""
            
        # 去掉?a=view部分
        if '?a=view' in filename:
            filename = filename.split('?a=view')[0]
        
        # URL解码
        try:
            filename = unquote(filename)
        except:
            pass
        
        # 去掉HTML标签
        filename = re.sub(r'<[^>]+>', '', filename)
        
        # 去掉路径信息，只保留文件名
        if '/' in filename:
            filename = filename.split('/')[-1]
        
        # 去掉多余的空白字符
        filename = filename.strip()
        
        # 确保以.mp4结尾
        if not filename.endswith('.mp4'):
            filename += '.mp4'
            
        return filename

    def _extract_anime_name(self, filename: str) -> str:
        """从文件名中提取番剧名称"""
        # 去掉[ANi]前缀
        name = re.sub(r'^\[ANi\]\s*', '', filename)
        
        # 去掉集数部分（如 - 01, - 02等）
        name = re.sub(r'\s*-\s*\d+\s*\[.*$', '', name)
        
        # 去掉画质信息
        name = re.sub(r'\s*\[.*$', '', name)
        
        return name.strip()

    def _extract_episode_number(self, filename: str) -> int:
        """从文件名中提取集数"""
        match = re.search(r'- (\d+)', filename)
        if match:
            return int(match.group(1))
        return 0

    @retry(Exception, tries=3, logger=logger, ret=[])
    def get_latest_list(self) -> List:
        addr = 'https://api.ani.rip/ani-download.xml'
        ret = RequestUtils(ua=settings.USER_AGENT if settings.USER_AGENT else None,
                           proxies=settings.PROXY if settings.PROXY else None).get_res(addr)
        ret_xml = ret.text
        ret_array = []
        # 解析XML
        dom_tree = xml.dom.minidom.parseString(ret_xml)
        rootNode = dom_tree.documentElement
        items = rootNode.getElementsByTagName("item")
        for item in items:
            rss_info = {}
            # 标题
            title = DomUtils.tag_value(item, "title", default="")
            # 链接
            link = DomUtils.tag_value(item, "link", default="")
            rss_info['title'] = title
            rss_info['link'] = link.replace("resources.ani.rip", "openani.an-i.workers.dev")
            ret_array.append(rss_info)
        return ret_array

    def __touch_strm_file(self, file_name, file_url: str = None) -> bool:
        if not file_url:
            # 季度API生成的URL，修复重复路径问题
            encoded_filename = quote(file_name, safe='.')
            # 修复URL格式，去掉重复的路径
            src_url = f'https://openani.an-i.workers.dev/{self._date}/{encoded_filename}?d=true'
        else:
            # 检查API获取的URL格式是否符合要求
            if self._is_url_format_valid(file_url):
                # 格式符合要求，直接使用
                src_url = file_url
            else:
                # 格式不符合要求，进行转换
                src_url = self._convert_url_format(file_url)
        
        # 清理文件名用于保存
        clean_file_name = self._clean_filename(file_name)
        file_path = f'{self._storageplace}/{clean_file_name}.strm'
        
        if os.path.exists(file_path):
            logger.debug(f'{clean_file_name}.strm 文件已存在')
            return False
        try:
            with open(file_path, 'w') as file:
                file.write(src_url)
                logger.debug(f'创建 {clean_file_name}.strm 文件成功')
                return True
        except Exception as e:
            logger.error('创建strm源文件失败：' + str(e))
            return False

    def _is_url_format_valid(self, url: str) -> bool:
        """检查URL格式是否符合要求（.mp4?d=true）"""
        return url.endswith('.mp4?d=true')

    def _convert_url_format(self, url: str) -> str:
        """将URL转换为符合要求的格式"""
        if '?d=mp4' in url:
            # 将 ?d=mp4 替换为 .mp4?d=true
            return url.replace('?d=mp4', '.mp4?d=true')
        elif url.endswith('.mp4'):
            # 如果已经以.mp4结尾，添加?d=true
            return f'{url}?d=true'
        else:
            # 其他情况，添加.mp4?d=true
            return f'{url}.mp4?d=true'

    def __task(self, fulladd: bool = False):
        cnt = 0
        # 增量添加更新
        if not fulladd:
            rss_info_list = self.get_latest_list()
            logger.info(f'本次处理 {len(rss_info_list)} 个文件')
            for rss_info in rss_info_list:
                if self.__touch_strm_file(file_name=rss_info['title'], file_url=rss_info['link']):
                    cnt += 1
        # 全量添加当季
        else:
            name_list = self.get_current_season_list()
            logger.info(f'本次处理 {len(name_list)} 个文件')
            for file_name in name_list:
                if self.__touch_strm_file(file_name=file_name):
                    cnt += 1
        logger.info(f'新创建了 {cnt} 个strm文件')

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'fulladd',
                                            'label': '下次创建当前季度所有番剧strm',
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 0 ? ? ?'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'storageplace',
                                            'label': 'Strm存储地址',
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '自动从open ANi抓取下载直链生成strm文件，免去人工订阅下载' + '\n' +
                                                    '配合目录监控使用，strm文件创建在/downloads/strm' + '\n' +
                                                    '通过目录监控转移到link媒体库文件夹 如/downloads/link/strm  mp会完成刮削',
                                            'style': 'white-space: pre-line;'
                                        }
                                    },
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'emby容器需要设置代理，docker的环境变量必须要有http_proxy代理变量，大小写敏感，具体见readme.' + '\n' +
                                                    'https://github.com/honue/MoviePilot-Plugins',
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
            "fulladd": False,
            "storageplace": '/downloads/strm',
            "cron": "*/20 22,23,0,1 * * *",
        }

    def __update_config(self):
        self.update_config({
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "enabled": self._enabled,
            "fulladd": self._fulladd,
            "storageplace": self._storageplace,
        })

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))


if __name__ == "__main__":
    top_anime_strm = TopAnimeStrm()
    name_list = top_anime_strm.get_latest_list()
    print(name_list)
