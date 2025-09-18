# main.py
from pathlib import Path
from datetime import datetime, time, timedelta, timezone
import json
import asyncio
import random
import os
from typing import List, Optional, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Plain
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain

@register("scheduled_reply", "tacbana", "一个强大的定时回复插件，允许您为不同群组设置在每天指定时间发送的特定消息", "1.0.0")
class ScheduledReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_data_dir = StarTools.get_data_dir()
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.storage_file = self.plugin_data_dir / "scheduled_reply_data.json"
        
        self.task: Optional[asyncio.Task] = None
        # 将白名单改为存储群号和对应回复内容的字典
        self.scheduled_replies: Dict[str, str] = {"早安岛":681772719}
        self.reply_statistics: Dict[str, Any] = {
            "total_replies": 0,
            "success_count": 0,
            "fail_count": 0,
            "last_reply_time": None
        }
        self.is_active = self.config.get("enable_auto_reply", True)
        self._stop_event = asyncio.Event()
        self.timezone = timezone(timedelta(hours=self.config.get("timezone", 8)))
        self._initialized = asyncio.Event()
        
        # 解析回复时间
        reply_time_str = self.config.get("reply_time", "09:00:00")
        
        try:
            hour, minute, second = map(int, reply_time_str.split(':'))
            self.reply_time = time(hour, minute, second)
        except:
            self.reply_time = time(9, 0, 0)
            logger.warning("定时回复时间格式错误，使用默认时间 09:00:00")
        
        asyncio.create_task(self._async_init())
    
    async def _async_init(self):
        await self._load_data()
        logger.info(
            f"定时回复插件初始化完成 | is_active={self.is_active} "
            f"| 已加载 {len(self.scheduled_replies)} 个定时任务"
        )
        if self.is_active:
            await self._start_reply_task()
        self._initialized.set()

    async def _load_data(self):
        """异步加载数据文件"""
        if not os.path.exists(self.storage_file):
            logger.info("数据文件不存在，将使用默认空配置。")
            return
        
        try:
            with open(self.storage_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.scheduled_replies = data.get("scheduled_replies", {})
                self.reply_statistics = data.get("reply_statistics", self.reply_statistics)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"加载数据文件失败: {e}", exc_info=True)

    async def _save_data(self) -> bool:
        """异步保存数据"""
        data_to_save = {
            "scheduled_replies": self.scheduled_replies,
            "reply_statistics": self.reply_statistics
        }
        try:
            # 使用异步文件操作写入
            temp_file = f"{self.storage_file}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, self.storage_file) # 原子操作
            logger.info("定时回复数据已保存。")
            return True
        except Exception as e:
            logger.error(f"保存数据文件失败: {e}", exc_info=True)
            return False

    def _get_local_time(self) -> datetime:
        return datetime.now(self.timezone)

    def _get_next_run_time(self) -> datetime:
        """计算下一次任务执行的本地时间"""
        now = self._get_local_time()
        if self.config.get("random", True):
            target_time = now.replace(
                hour=self.reply_time.hour,
                minute=random.randint(0, 59),
                second=random.randint(0, 59),
                microsecond=0
            )
        else:
            target_time = now.replace(
                hour=self.reply_time.hour,
                minute=self.reply_time.minute,
                second=self.reply_time.second,
                microsecond=0
            )

        if now >= target_time:
            target_time += timedelta(days=1)
        return target_time

    async def _start_reply_task(self):
        """启动定时回复任务"""
        if self.is_active and (self.task is None or self.task.done()):
            self._stop_event.clear()
            self.task = asyncio.create_task(self._daily_reply_task())
            logger.info("自动回复任务已启动。")

    async def _send_scheduled_reply(self, group_id: str, message: str) -> dict:
        """执行单次定时回复"""
        try:
            session_str = f"default:GroupMessage:{group_id}"
            message_chain = MessageChain().message(message)
            await self.context.send_message(session_str, message_chain)
            logger.info(f"向群 {group_id} 发送定时回复成功。")
            return {"success": True, "message": "发送成功"}
        except Exception as e:
            error_msg = f"向群 {group_id} 发送定时回复失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}

    async def _notify_admin(self, message: str):
        """通知管理员"""
        if not self.config.get("admin_notification", True):
            return
        admin_group_id = self.config.get("admin_group_id")
        if not admin_group_id:
            return
        
        try:
            notification_msg = f"定时回复通知\n{message}"
            session_str = f"aiocqhttp:GROUP:{admin_group_id}"
            await self.context.send_message(session_str, [Plain(notification_msg)])
        except Exception as e:
            logger.error(f"通知管理员失败: {e}", exc_info=True)

    async def _execute_all_replies(self) -> str:
        """执行所有已配置的定时回复"""
        targets = self.scheduled_replies
        if not targets:
            return "没有配置任何定时回复任务。"
        
        tasks = [self._send_scheduled_reply(gid, msg) for gid, msg in targets.items()]
        results = await asyncio.gather(*tasks)
        
        success_count = sum(1 for r in results if r["success"])
        fail_count = len(results) - success_count
        
        # 更新统计信息
        self.reply_statistics["total_replies"] += len(results)
        self.reply_statistics["success_count"] += success_count
        self.reply_statistics["fail_count"] += fail_count
        self.reply_statistics["last_reply_time"] = datetime.now().isoformat()
        await self._save_data()
        
        summary = f" 本次定时回复任务完成\n 成功: {success_count}\n 失败: {fail_count}"
        await self._notify_admin(summary)
        return summary

    async def _daily_reply_task(self):
        """每日定时回复的核心循环任务"""
        try:
            while not self._stop_event.is_set():
                try:
                    target_time = self._get_next_run_time()
                    now = self._get_local_time()
                    wait_seconds = (target_time - now).total_seconds()
                    
                    logger.info(f"距离下次定时回复还有 {wait_seconds:.1f} 秒 (将在 {target_time.strftime('%Y-%m-%d %H:%M:%S')} 执行)")
                    
                    try:
                        # 等待直到指定时间，或直到停止事件被设置
                        await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                        if self._stop_event.is_set():
                            break # 如果是停止事件触发的，就退出循环
                    except asyncio.TimeoutError:
                        # 这是正常情况，意味着等待时间已到
                        pass
                    
                    logger.info("开始执行每日定时回复...")
                    await self._execute_all_replies()
                    await asyncio.sleep(1) # 短暂休眠，防止CPU占用过高并确保进入下一天的计时

                except Exception as e:
                    logger.error(f"定时回复任务内部循环出错: {e}", exc_info=True)
                    await self._notify_admin(f"定时回复任务发生严重错误: {e}")
                    await asyncio.sleep(60) # 发生错误后等待60秒再重试
        except asyncio.CancelledError:
            logger.info("自动回复任务被取消。")
        except Exception as e:
            logger.error(f"自动回复任务异常终止: {e}", exc_info=True)

    @filter.command("回复菜单")
    async def reply_menu(self, event: AstrMessageEvent):
        """显示插件的所有可用指令"""
        menu_text = """
定时回复插件指令菜单

添加/删除任务：
• /添加回复 [群号] [回复内容] - 为指定群添加定时回复
• /移除回复 [群号] - 删除指定群的定时回复

任务管理：
• /开启自动回复 - 启动每日定时回复
• /关闭自动回复 - 停止每日定时回复
• /设置回复时间 [HH:MM:SS] - 设置每日回复时间

状态与查看：
• /查看回复 - 查看所有已配置的定时回复任务
• /回复状态 - 查看插件的当前状态和统计
• /立即执行 - 手动触发一次所有定时回复

提示：
• [群号] 请填写完整的QQ群号
• [回复内容] 可以包含空格
        """
        yield event.chain_result([Plain(menu_text)])

    @filter.command("添加回复")
    async def add_scheduled_reply(self, event: AstrMessageEvent, group_id: str, *, message: str):
        """为指定群添加或更新定时回复"""
        await self._initialized.wait()
        group_id = group_id.strip()
        message = message.strip()
        if not group_id.isdigit():
            yield event.chain_result([Plain("群号格式不正确，应为纯数字。")])
            return
        
        self.scheduled_replies[group_id] = message
        if await self._save_data():
            yield event.chain_result([Plain(f"操作成功！\n群聊 {group_id} 的定时回复已设置为：\n{message}")])
        else:
            yield event.chain_result([Plain("添加失败，保存数据时发生错误。")])

    @filter.command("移除回复")
    async def remove_scheduled_reply(self, event: AstrMessageEvent, group_id: str):
        """删除指定群的定时回复"""
        await self._initialized.wait()
        group_id = group_id.strip()
        if group_id in self.scheduled_replies:
            self.scheduled_replies.pop(group_id)
            await self._save_data()
            yield event.chain_result([Plain(f"已成功移除群 {group_id} 的定时回复任务。")])
        else:
            yield event.chain_result([Plain(f"未找到群 {group_id} 的定时回复任务。")])

    @filter.command("查看回复")
    async def view_scheduled_replies(self, event: AstrMessageEvent):
        """查看所有已配置的定时回复任务"""
        await self._initialized.wait()
        if not self.scheduled_replies:
            yield event.chain_result([Plain("当前没有配置任何定时回复任务。")])
            return
        
        reply_list = ["当前已配置的定时回复任务："]
        for group_id, message in self.scheduled_replies.items():
            reply_list.append(f"\n- 群 {group_id} -> “{message}”")
        
        yield event.chain_result([Plain("\n".join(reply_list))])

    @filter.command("回复状态")
    async def reply_status(self, event: AstrMessageEvent):
        """查看插件状态和统计"""
        await self._initialized.wait()
        status = "自动回复已开启" if self.is_active else "自动回复已停止"
        next_run_time = self._get_next_run_time()
        
        stats = self.reply_statistics
        stats_msg = (
            f"回复统计:\n"
            f"总计: {stats['total_replies']}\n"
            f"成功: {stats['success_count']}\n"
            f"失败: {stats['fail_count']}"
        )
        if stats['last_reply_time']:
            stats_msg += f"\n上次执行: {stats['last_reply_time']}"
        
        message = (
            f"{status}\n"
            f"⏰ 每日回复时间: {self.reply_time.strftime('%H:%M:%S')} (UTC+{self.config.get('timezone', 8)})\n"
            f"⏱ 下次执行时间: {next_run_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{stats_msg}"
        )
        yield event.chain_result([Plain(message)])
        
    @filter.command("立即执行")
    async def manual_execute_replies(self, event: AstrMessageEvent):
        """手动触发一次所有回复任务"""
        await self._initialized.wait()
        yield event.chain_result([Plain("正在手动执行所有定时回复任务...")])
        result_summary = await self._execute_all_replies()
        yield event.chain_result([Plain(result_summary)])

    # --- 以下是控制插件启停和设置时间的指令 ---

    @filter.command("开启自动回复")
    async def start_auto_reply(self, event: AstrMessageEvent):
        await self._initialized.wait()
        if self.is_active:
            yield event.chain_result([Plain("自动回复任务已经在运行中。")])
            return
            
        self.is_active = True
        self.config["enable_auto_reply"] = True
        self.config.save_config()
        await self._start_reply_task()
        yield event.chain_result([Plain("自动回复已开启。")])

    @filter.command("关闭自动回复")
    async def stop_auto_reply(self, event: AstrMessageEvent):
        await self._initialized.wait()
        if not self.is_active:
            yield event.chain_result([Plain("自动回复任务并未运行。")])
            return
            
        self.is_active = False
        self.config["enable_auto_reply"] = False
        self.config.save_config()
        self._stop_event.set()
        if self.task:
            self.task.cancel()
            self.task = None
        yield event.chain_result([Plain("自动回复已停止。")])

    @filter.command("设置回复时间")
    async def set_reply_time(self, event: AstrMessageEvent, time_str: str):
        await self._initialized.wait()
        try:
            new_time = time.fromisoformat(time_str)
            self.reply_time = new_time
            self.config["reply_time"] = time_str
            self.config.save_config()
            # 重启任务以应用新时间
            if self.is_active:
                self._stop_event.set()
                if self.task:
                    self.task.cancel()
                await self._start_reply_task()
            yield event.chain_result([Plain(f"回复时间已设置为每天 {time_str}")])
        except ValueError:
            yield event.chain_result([Plain("时间格式错误，请使用 HH:MM:SS 格式。")])

    async def terminate(self):
        """插件终止时执行清理"""
        self._stop_event.set()
        if self.task and not self.task.done():
            self.task.cancel()
        logger.info("定时回复插件已终止。")