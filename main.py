import aiohttp
import asyncio
import time
from typing import Optional, Dict, Any, List
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image, Plain, Reply


@register(
    "search_tracemoeX",
    "PaloMiku / GitHub Copilot (Claude Haiku 4.5)",
    "基于 Trace.moe API 的动漫截图场景识别插件（增强版）",
    "1.0.8"
)
class TraceMoePlugin(Star):
    """TraceMoe 动漫场景识别插件主类"""

    # ============== 初始化和生命周期 ==============
    
    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化插件实例"""
        super().__init__(context)
        
        # API 配置
        self.api_base = config.get("api_base", "https://api.trace.moe")
        self.api_key = config.get("api_key", "").strip()
        
        # 搜索结果配置
        self.max_results = config.get("max_results", 3)
        if self.max_results < 1:
            self.max_results = 1
        elif self.max_results > 10:
            self.max_results = 10
        self.enable_preview = config.get("enable_preview", True)
        
        # 网络会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        # 等待模式管理
        self.user_states = {}  # 用户状态: {session_key: {"step": "...", "timestamp": ...}}
        self.cleanup_task = None  # 定时清理任务
        self.search_params_timeout = config.get("search_params_timeout", 30)  # 等待超时（秒）
        
        # 状态处理器映射
        self.state_handlers = {
            "waiting_image": self._handle_waiting_image,
        }
        
        # 超时任务映射
        self.timeout_tasks = {}  # {session_key: asyncio.Task}
        
        # 日志输出
        auth_mode = "API 密钥" if self.api_key else "访客模式"
        preview_status = "启用" if self.enable_preview else "禁用"
        logger.info(
            f"TraceMoe 插件已加载 | "
            f"API: {self.api_base} | "
            f"最大结果: {self.max_results} | "
            f"认证: {auth_mode} | "
            f"预览: {preview_status}"
        )

    async def initialize(self):
        """初始化 HTTP 会话和定时清理任务"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "AstrBot-TraceMoe-Plugin/1.0.8"}
        )
        # 启动定时清理超时用户状态的任务
        self.cleanup_task = asyncio.create_task(self.cleanup_loop())
        logger.info("TraceMoe 插件初始化完成")

    async def terminate(self):
        """清理资源和定时任务"""
        if self.session and not self.session.closed:
            await self.session.close()
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
        # 清理所有超时任务
        for task in self.timeout_tasks.values():
            if not task.done():
                task.cancel()
        self.timeout_tasks.clear()
        logger.info("TraceMoe 插件已停止")

    # ============== 网络请求 ==============
    
    async def _ensure_session(self):
        """确保 HTTP 会话已初始化"""
        if not self.session or self.session.closed:
            await self.initialize()

    def _build_headers(self) -> Dict[str, str]:
        """构建请求头"""
        headers = {}
        if self.api_key:
            headers["x-trace-key"] = self.api_key
        return headers

    def _handle_http_error(self, status_code: int, operation: str = "请求") -> str:
        """统一处理 HTTP 错误状态码"""
        error_map = {
            400: "无效的请求数据或处理失败",
            402: "触及 API 并发限制或配额用尽",
            403: "无效的 API 密钥或无权限访问",
            404: "资源不存在或已失效",
            413: "文件过大（超过25MB）",
            429: "请求过于频繁，请稍后再试",
            503: "服务暂时不可用，请稍后再试"
        }
        if status_code in error_map:
            return error_map[status_code]
        elif status_code >= 500:
            return "服务器内部错误，请稍后再试"
        else:
            return f"{operation}失败，HTTP状态码: {status_code}"

    async def _download_img(self, url: str) -> Optional[bytes]:
        """异步下载图片数据"""
        try:
            async with self.session.get(url, timeout=15) as response:
                if response.status == 200:
                    return await response.read()
        except Exception as e:
            logger.warning(f"下载图片失败: {e}")
        return None

    async def search_by_image_data(self, image_data: bytes, cut_borders: bool = False) -> Dict[str, Any]:
        """通过图片二进制数据搜索动漫"""
        await self._ensure_session()
        
        params = {"anilistInfo": ""}
        if cut_borders:
            params["cutBorders"] = ""
            
        search_url = f"{self.api_base}/search"
        headers = self._build_headers()
        
        form_data = aiohttp.FormData()
        form_data.add_field("image", image_data, content_type="image/jpeg")
        
        try:
            async with self.session.post(
                search_url, 
                params=params,
                data=form_data,
                headers=headers
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("error"):
                        raise ValueError(f"API 错误: {result['error']}")
                    return result
                else:
                    raise ValueError(self._handle_http_error(response.status, "搜索"))
        except (aiohttp.ClientTimeout, aiohttp.ClientError) as e:
            error_type = "请求超时" if isinstance(e, aiohttp.ClientTimeout) else "网络连接错误"
            raise ValueError(f"{error_type}，请稍后再试")

    async def get_user_quota(self) -> Dict[str, Any]:
        """获取用户 API 配额信息"""
        await self._ensure_session()
        me_url = f"{self.api_base}/me"
        headers = self._build_headers()
            
        try:
            async with self.session.get(me_url, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise ValueError(self._handle_http_error(response.status, "查询配额"))
        except (aiohttp.ClientTimeout, aiohttp.ClientError) as e:
            error_type = "查询超时" if isinstance(e, aiohttp.ClientTimeout) else "网络连接错误"
            raise ValueError(f"{error_type}，请稍后再试")

    # ============== 消息解析 ==============
    
    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """获取会话唯一标识"""
        try:
            return str(event.get_sender_id())
        except Exception:
            return "unknown_session"
    
    def get_message_text(self, message_obj) -> str:
        """提取消息对象中的文本内容"""
        try:
            raw_message = getattr(message_obj, 'raw_message', '')
            if isinstance(raw_message, str):
                return raw_message.strip()
            elif isinstance(raw_message, dict) and "message" in raw_message:
                texts = [
                    msg_part.get("data", {}).get("text", "")
                    for msg_part in raw_message.get("message", [])
                    if msg_part.get("type") == "text"
                ]
                return " ".join(texts).strip()
        except Exception:
            pass
        return ''

    async def download_image_from_component(self, image_component: Image) -> bytes:
        """从图片组件下载图片数据"""
        await self._ensure_session()
        
        if not (hasattr(image_component, 'url') and image_component.url):
            raise ValueError("无法获取图片数据")
            
        try:
            async with self.session.get(image_component.url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    if len(image_data) > 25 * 1024 * 1024:  # 25MB
                        raise ValueError("图片文件过大（超过25MB）")
                    return image_data
                else:
                    raise ValueError(self._handle_http_error(response.status, "下载图片"))
        except (aiohttp.ClientTimeout, aiohttp.ClientError) as e:
            error_type = "下载超时" if isinstance(e, aiohttp.ClientTimeout) else "网络连接错误"
            raise ValueError(f"{error_type}，请稍后再试")

    async def _get_image_from_reply(self, event: AstrMessageEvent) -> Optional[bytes]:
        """从引用的消息中获取图片"""
        try:
            messages = event.get_messages()
            for msg in messages:
                if isinstance(msg, Reply) and hasattr(msg, 'chain') and msg.chain:
                    for reply_msg in msg.chain:
                        if isinstance(reply_msg, Image):
                            try:
                                img_bytes = await self.download_image_from_component(reply_msg)
                                if img_bytes:
                                    logger.info("✓ 成功下载引用消息中的图片")
                                    return img_bytes
                            except Exception as e:
                                logger.warning(f"下载引用消息中的图片失败: {e}")
            return None
        except Exception as e:
            logger.error(f"提取引用消息图片异常: {e}", exc_info=True)
            return None

    # ============== 结果格式化 ==============
    
    def format_time(self, seconds: float) -> str:
        """将秒数格式化为时分秒"""
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        else:
            return f"{m:02d}:{s:02d}"

    async def format_search_result(self, result_data: Dict[str, Any]) -> List:
        """格式化搜索结果为消息链"""
        
        if result_data.get("error"):
            return [Plain(f"搜索出错: {result_data['error']}")]
            
        results = result_data.get("result", [])
        if not results:
            return [Plain("未找到匹配的动漫场景")]
            
        message_chain = []
        
        # 添加预览图片
        if self.enable_preview and results:
            first_result = results[0]
            try:
                if first_result.get("image"):
                    image_url = first_result["image"] + "?size=m"  # 中等尺寸
                    message_chain.append(Image.fromURL(image_url))
            except Exception as e:
                logger.warning(f"加载预览图片失败: {e}")
        
        # 添加文本结果
        output_lines = ["🔍 动漫场景识别结果：\n"]
        
        for i, result in enumerate(results[:self.max_results], 1):
            similarity = result.get("similarity", 0) * 100
            
            # 获取动漫标题
            anilist_info = result.get("anilist")
            if isinstance(anilist_info, dict):
                title_info = anilist_info.get("title", {})
                anime_title = (
                    title_info.get("native") or 
                    title_info.get("romaji") or 
                    title_info.get("english") or 
                    "未知动漫"
                )
                mal_id = anilist_info.get("idMal")
                mal_link = f"\n📺 MyAnimeList: https://myanimelist.net/anime/{mal_id}" if mal_id else ""
            else:
                anime_title = f"AniList ID: {anilist_info}"
                mal_link = ""

            # 拼接结果文本
            result_text = f"#{i} 【{anime_title}】\n"
            result_text += f"📊 相似度: {similarity:.1f}%\n"
            result_text += f"⏰ 时间: {self.format_time(result.get('at', 0))}"
            
            from_time = result.get("from", 0)
            to_time = result.get("to", 0)
            if from_time != to_time:
                result_text += f" ({self.format_time(from_time)}-{self.format_time(to_time)})"
                
            result_text += f"\n📁 文件: {result.get('filename', '未知')}"
            
            episode = result.get("episode")
            if episode:
                result_text += f"\n📺 集数: 第{episode}集"
                
            result_text += mal_link + "\n"
            output_lines.append(result_text)
            
        # 添加页脚
        footer = f"\n💡 搜索了 {result_data.get('frameCount', 0):,} 帧画面"
        footer += "\n⚠️ 相似度低于90%的结果可能不准确"
        output_lines.append(footer)
        
        message_chain.append(Plain("\n".join(output_lines)))
        
        return message_chain

    # ============== 状态管理 ==============
    
    async def cleanup_loop(self):
        """定时清理超时无响应的用户状态数据"""
        while True:
            try:
                await asyncio.sleep(600)  # 每10分钟清理一次
                now = time.time()
                to_delete = [
                    session_key for session_key, state in list(self.user_states.items())
                    if now - state.get('timestamp', now) > self.search_params_timeout
                ]
                for session_key in to_delete:
                    del self.user_states[session_key]
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"清理用户状态失败: {e}")

    def _clear_waiting_states_before_search(self, session_key: str):
        """在执行搜索前清除用户等待状态"""
        if session_key in self.user_states:
            del self.user_states[session_key]
        # 取消超时任务
        if session_key in self.timeout_tasks:
            self.timeout_tasks[session_key].cancel()
            del self.timeout_tasks[session_key]

    async def _timeout_check(self, session_key: str):
        """超时检查任务"""
        try:
            await asyncio.sleep(self.search_params_timeout)
            if session_key in self.user_states:
                # 超时后仍然在等待，发送超时消息
                session = self.user_states[session_key]
                event = session.get("event")
                del self.user_states[session_key]
                if session_key in self.timeout_tasks:
                    del self.timeout_tasks[session_key]
                try:
                    if event:
                        await event.send(event.plain_result("⏱️ 等待超时，操作已取消\n请重新发送 /搜番 命令"))
                        logger.info(f"会话 {session_key} 等待超时，已发送超时消息")
                except Exception as send_error:
                    logger.warning(f"发送超时消息失败: {send_error}")
        except asyncio.CancelledError:
            # 任务被取消，说明用户已经发送了图片
            pass
        except Exception as e:
            logger.warning(f"超时任务异常: {e}")

    # ============== 消息处理器 ==============
    
    async def _get_image_data_by_priority(self, event: AstrMessageEvent) -> Optional[bytes]:
        """按优先级获取图片数据（直接消息 > 引用消息）"""
        images = [comp for comp in event.get_messages() if isinstance(comp, Image)]
        img_data = None
        
        # 1. 直接发送的图片文件
        if images:
            try:
                logger.info("→ 尝试优先级1：从直接消息提取图片...")
                img_data = await self.download_image_from_component(images[0])
                logger.info("✓ 优先级1成功")
                return img_data
            except Exception as e:
                logger.warning(f"✗ 优先级1失败: {e}")
        
        # 2. 引用的消息中的图片
        logger.info("→ 尝试优先级2：从引用消息提取图片...")
        img_data = await self._get_image_from_reply(event)
        if img_data:
            logger.info("✓ 优先级2成功")
            return img_data
        else:
            logger.info("✗ 优先级2失败或无引用消息")
            return None

    async def _handle_waiting_image(self, event: AstrMessageEvent, state: dict, session_key: str):
        """处理用户在等待图片输入状态中的消息"""
        # 清除等待状态，防止重复触发
        if session_key in self.user_states:
            del self.user_states[session_key]
        if session_key in self.timeout_tasks:
            self.timeout_tasks[session_key].cancel()
            del self.timeout_tasks[session_key]
            
        img_buffer = await self._get_image_data_by_priority(event)
        
        if img_buffer:
            try:
                logger.info(f"会话 {session_key} 开始搜索")
                yield event.plain_result("🔍 正在搜索动漫场景，请稍候...")
                result = await self.search_by_image_data(img_buffer, cut_borders=True)
                formatted_result = await self.format_search_result(result)
                yield event.chain_result(formatted_result)
            except Exception as e:
                logger.error(f"搜索失败: {e}")
                yield event.plain_result(f"❌ 搜索失败: {str(e)}")
            event.stop_event()
        else:
            logger.info(f"会话 {session_key} 的等待消息未包含图片")
            yield event.plain_result("请发送一张图片")
            # 恢复等待状态供继续等待
            self.user_states[session_key] = {
                "step": "waiting_image",
                "timestamp": time.time(),
                "event": event,
            }
            timeout_task = asyncio.create_task(self._timeout_check(session_key))
            self.timeout_tasks[session_key] = timeout_task
            event.stop_event()

    @filter.command("搜番")
    async def search_anime(self, event: AstrMessageEvent):
        """主命令：搜索动漫场景"""
        async for result in self._handle_search_request(event):
            yield result

    async def _handle_search_request(self, event: AstrMessageEvent):
        """
        处理搜索请求的主逻辑
        
        流程：
        1. 检查是否有图片（直接、引用）
        2. 有图片 → 立即搜索
        3. 无图片 → 进入等待模式
        """
        session_key = self._get_session_key(event)
        
        # 清除该用户的任何现有等待状态，防止重复触发
        self._clear_waiting_states_before_search(session_key)
        
        try:
            img_data = await self._get_image_data_by_priority(event)
            
            # 执行搜索或进入等待模式
            if img_data:
                logger.info(f"图片获取成功，开始搜索")
                yield event.plain_result("🔍 正在搜索动漫场景，请稍候...")
                result = await self.search_by_image_data(img_data, cut_borders=True)
                formatted_result = await self.format_search_result(result)
                yield event.chain_result(formatted_result)
            else:
                # 进入等待模式
                logger.info(f"所有优先级都未获取到图片，进入等待模式")
                self.user_states[session_key] = {
                    "step": "waiting_image",
                    "timestamp": time.time(),
                    "event": event,
                }
                timeout_task = asyncio.create_task(self._timeout_check(session_key))
                self.timeout_tasks[session_key] = timeout_task
                
                yield event.plain_result(
                    "🖼️ 已进入等待模式，请发送图片来搜索动漫\n\n"
                    "支持的方式：\n"
                    "✅ 发送图片文件（自动裁切黑边）\n"
                    "✅ 引用有图片的消息\n\n"
                    "📋 图片要求：\n"
                    "• 格式：jpg, png, gif, webp 等\n"
                    "• 推荐尺寸：640x360px\n"
                    "• 大小限制：25MB\n\n"
                    f"⏰ 等待倒计时：{self.search_params_timeout}秒（超时自动取消）\n"
                    "💡 帮助：/搜番帮助"
                )
            
        except ValueError as e:
            logger.warning(f"搜索请求处理 - ValueError: {e}")
            yield event.plain_result(f"❌ 搜索失败: {str(e)}")
        except Exception as e:
            logger.error(f"TraceMoe 搜索出现未知错误: {e}", exc_info=True)
            yield event.plain_result("❌ 搜索时发生未知错误，请稍后再试")
        finally:
            event.stop_event()

    @filter.command("搜番帮助")
    async def show_info(self, event: AstrMessageEvent):
        """显示使用帮助"""
        info_text = """🎌 TraceMoe 动漫场景识别插件

📝 功能说明
通过图片识别动漫截图，自动裁切黑边

🎯 使用方法
• /搜番 + 图片 - 直接搜索
• 引用消息 + /搜番 - 搜索引用消息中的图片
• /搜番 - 进入等待模式，{self.search_params_timeout}秒内发送图片

💡 支持格式：jpg, png, gif, webp，≤25MB

📊 结果说明
相似度 ≥90% - 准确
相似度 70-89% - 参考
相似度 <70% - 可能不准确

🔧 管理员命令：/搜番配额"""

        yield event.plain_result(info_text)
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("搜番配额")
    async def show_quota(self, event: AstrMessageEvent):
        """查询 API 使用配额（仅管理员）"""
        try:
            yield event.plain_result("🔍 正在查询 API 使用配额...")
            
            quota_data = await self.get_user_quota()
            user_id = quota_data.get("id", "未知")
            
            # 安全数据转换
            def safe_int(value, default):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
                    
            priority = safe_int(quota_data.get("priority"), 0)
            concurrency = safe_int(quota_data.get("concurrency"), 1)
            quota = safe_int(quota_data.get("quota"), 0)
            quota_used = safe_int(quota_data.get("quotaUsed"), 0)
            
            quota_remaining = quota - quota_used
            usage_rate = (quota_used / quota * 100) if quota > 0 else 0
            
            quota_info = f"""📊 TraceMoe API 配额信息

⚡ 优先级: {priority} (0为最低优先级)
🔄 并发限制: {concurrency} 个请求
📈 月度配额: {quota:,} 次
✅ 已使用: {quota_used:,} 次
💚 剩余配额: {quota_remaining:,} 次

📊 使用率: {usage_rate:.1f}%"""

            if self.api_key:
                quota_info += "\n🔑 使用 API 密钥认证"
            else:
                masked_ip = user_id[:8] + "****" if len(user_id) > 12 else "****"
                quota_info += f"\n🌐 访客模式 (IP: {masked_ip})"
                
            yield event.plain_result(quota_info)
            event.stop_event()
            
        except ValueError as e:
            logger.warning(f"TraceMoe 配额查询失败: {e}")
            yield event.plain_result(f"❌ 查询配额失败: {str(e)}")
            event.stop_event()
        except Exception as e:
            logger.error(f"TraceMoe 配额查询出现未知错误: {e}", exc_info=True)
            yield event.plain_result("❌ 查询配额时发生未知错误，请稍后再试")
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """
        全局消息处理器
        
        用于处理用户在等待模式中的消息
        注意：不处理命令事件（如/搜番），避免重复触发
        """
        session_key = self._get_session_key(event)
        state = self.user_states.get(session_key)
        
        # 如果用户不在等待状态，则不处理
        if not state:
            return
        
        # 跳过命令事件，避免与@filter.command冲突
        message_text = self.get_message_text(event.message_obj)
        if message_text.startswith('/'):
            return
        
        # 根据等待状态分发处理
        handler = self.state_handlers.get(state.get("step"))
        if handler:
            async for result in handler(event, state, session_key):
                yield result