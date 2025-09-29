# main.py
from pathlib import Path
from datetime import datetime, time, timedelta, timezone
import json
import asyncio
import random
import os
import aiohttp
from typing import List, Optional, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Plain
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain

@register("scheduled_reply", "tacbana", "astrbot的定时回复插件，让你的机器人每天早上八点在群里发早上好", "1.1.0")
class ScheduledReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_data_dir = StarTools.get_data_dir()
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.storage_file = self.plugin_data_dir / "scheduled_reply_data.json"
        
        self.tasks: Dict[str, asyncio.Task] = {}
        # 群号 -> 任务列表
        self.scheduled_tasks: Dict[str, List[Dict[str, Any]]] = {}
        self.reply_statistics: Dict[str, Any] = {
            "total_replies": 0,
            "success_count": 0,
            "fail_count": 0,
            "last_reply_time": None
        }
        self.is_active = self.config.get("enable_auto_reply", True)
        self._stop_events: Dict[str, asyncio.Event] = {}
        self.timezone = timezone(timedelta(hours=self.config.get("timezone", 8)))
        self._initialized = asyncio.Event()
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        asyncio.create_task(self._async_init())
    
    async def _async_init(self):
        await self._load_data()
        # 初始化HTTP会话
        self.http_session = aiohttp.ClientSession()
        logger.info(
            f"定时回复插件初始化完成 | is_active={self.is_active} "
            f"| 已加载 {sum(len(tasks) for tasks in self.scheduled_tasks.values())} 个定时任务"
        )
        if self.is_active:
            await self._start_all_tasks()
        self._initialized.set()

    async def _load_data(self):
        """异步加载数据文件"""
        try:
            with open(self.storage_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 兼容旧版本数据格式
                if "scheduled_replies" in data and isinstance(data["scheduled_replies"], dict):
                    # 迁移旧数据到新格式
                    self.scheduled_tasks = self._migrate_old_data(data["scheduled_replies"])
                else:
                    self.scheduled_tasks = data.get("scheduled_tasks", {})
                self.reply_statistics = data.get("reply_statistics", self.reply_statistics)
        except FileNotFoundError:
            logger.warning("数据文件不存在，将创建新文件")
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
        except OSError as e:
            logger.error(f"读取文件失败: {e}")

    def _migrate_old_data(self, old_data: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
        """迁移旧版本数据到新格式"""
        new_data = {}
        default_time = self.config.get("reply_time", "09:00:00")
        for group_id, message in old_data.items():
            new_data[group_id] = [{
                "task_id": f"migrated_{group_id}",
                "time": default_time,
                "content": message,
                "content_type": "static",
                "created_time": datetime.now().isoformat()
            }]
        logger.info(f"已迁移 {len(new_data)} 个旧版定时任务到新格式")
        return new_data

    async def _save_data(self) -> bool:
        """异步保存数据"""
        data_to_save = {
            "scheduled_tasks": self.scheduled_tasks,
            "reply_statistics": self.reply_statistics
        }
        try:
            temp_file = f"{self.storage_file}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, self.storage_file)
            logger.info("定时回复数据已保存。")
            return True
        except OSError as e:
            logger.error(f"写入文件失败: {e}", exc_info=True)
            return False

    def _get_local_time(self) -> datetime:
        return datetime.now(self.timezone)

    def _parse_time(self, time_str: str) -> time:
        """解析时间字符串"""
        try:
            parts = time_str.split(':')
            if len(parts) == 2:
                hour, minute = map(int, parts)
                return time(hour, minute, 0)
            elif len(parts) == 3:
                hour, minute, second = map(int, parts)
                return time(hour, minute, second)
            else:
                raise ValueError("时间格式错误")
        except ValueError:
            raise ValueError(f"时间格式错误: {time_str}，请使用 HH:MM 或 HH:MM:SS 格式")

    def _get_next_run_time(self, task_time: str) -> datetime:
        """计算下一次任务执行的本地时间"""
        now = self._get_local_time()
        target_time_obj = self._parse_time(task_time)
        
        if False:
            target_time = now.replace(
                hour=target_time_obj.hour,
                minute=random.randint(0, 59),
                second=random.randint(0, 59),
                microsecond=0
            )
        else:
            target_time = now.replace(
                hour=target_time_obj.hour,
                minute=target_time_obj.minute,
                second=target_time_obj.second or 0,
                microsecond=0
            )

        if now >= target_time:
            target_time += timedelta(days=1)
        return target_time

    async def _fetch_dynamic_content(self, url: str, auth_header: str = "") -> str:
        """获取动态内容"""
        if not self.http_session:
            self.http_session = aiohttp.ClientSession()
        
        headers = {}
        if auth_header:
            try:
                auth_data = json.loads(auth_header)
                headers.update(auth_data)
            except json.JSONDecodeError:
                # 如果不是JSON格式，直接作为Authorization头
                headers["Authorization"] = auth_header
        
        try:
            async with self.http_session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    content = await response.text()
                    return content.strip()
                else:
                    raise Exception(f"HTTP {response.status}: {await response.text()}")
        except asyncio.TimeoutError:
            raise Exception("请求超时")
        except Exception as e:
            raise Exception(f"获取动态内容失败: {str(e)}")

    async def _get_task_content(self, task: Dict[str, Any]) -> str:
        """获取任务内容（静态或动态）"""
        content_type = task.get("content_type", "static")
        
        if content_type == "static":
            return task["content"]
        elif content_type == "dynamic":
            url = task["content"]
            auth_header = task.get("auth_header", "")
            try:
                return await self._fetch_dynamic_content(url, auth_header)
            except Exception as e:
                logger.error(f"获取动态内容失败: {e}")
                return f"[动态内容获取失败: {str(e)}]"
        else:
            return "[未知内容类型]"

    async def _send_scheduled_reply(self, group_id: str, task: Dict[str, Any]) -> dict:
        """执行单次定时回复"""
        try:
            content = await self._get_task_content(task)
            session_str = f"default:GroupMessage:{group_id}"
            message_chain = MessageChain().message(content)
            await self.context.send_message(session_str, message_chain)
            logger.info(f"向群 {group_id} 发送定时回复成功 (任务: {task.get('task_id', 'unknown')})")
            return {"success": True, "message": "发送成功"}
        except Exception as e:
            error_msg = f"向群 {group_id} 发送定时回复失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}

    async def _start_task_for_group(self, group_id: str):
        """为指定群组启动所有定时任务"""
        if group_id not in self.scheduled_tasks:
            return
            
        self._stop_events[group_id] = asyncio.Event()
        task_id = f"group_{group_id}"
        
        if task_id in self.tasks:
            self.tasks[task_id].cancel()
        
        self.tasks[task_id] = asyncio.create_task(self._group_reply_task(group_id))

    async def _group_reply_task(self, group_id: str):
        """单个群组的定时回复任务"""
        stop_event = self._stop_events[group_id]
        
        try:
            while not stop_event.is_set() and group_id in self.scheduled_tasks:
                tasks = self.scheduled_tasks[group_id]
                if not tasks:
                    break
                
                # 计算下一个最近的任务时间
                next_times = [self._get_next_run_time(task["time"]) for task in tasks]
                next_time = min(next_times)
                next_task_index = next_times.index(next_time)
                next_task = tasks[next_task_index]
                
                now = self._get_local_time()
                wait_seconds = (next_time - now).total_seconds()
                if wait_seconds > 0:
                    if self.config.get("random", True):
                        wait_seconds += random.randint(0, 3600)
                    logger.info(f"群 {group_id} 距离下次定时回复还有 {wait_seconds:.1f} 秒 (任务: {next_task.get('task_id', 'unknown')})")
                    
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                        if stop_event.is_set():
                            break
                    except asyncio.TimeoutError:
                        pass
                
                # 执行任务
                result = await self._send_scheduled_reply(group_id, next_task)
                
                # 更新统计
                self.reply_statistics["total_replies"] += 1
                if result["success"]:
                    self.reply_statistics["success_count"] += 1
                else:
                    self.reply_statistics["fail_count"] += 1
                self.reply_statistics["last_reply_time"] = datetime.now().isoformat()
                await self._save_data()
                
                # 短暂休眠防止重复执行
                await asyncio.sleep(1)
                
        except asyncio.CancelledError:
            logger.info(f"群 {group_id} 的定时回复任务被取消")
        except Exception as e:
            logger.error(f"群 {group_id} 的定时回复任务异常: {e}", exc_info=True)

    async def _start_all_tasks(self):
        """启动所有群组的定时任务"""
        for group_id in self.scheduled_tasks.keys():
            await self._start_task_for_group(group_id)

    async def _stop_all_tasks(self):
        """停止所有定时任务"""
        for event in self._stop_events.values():
            event.set()
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
        self._stop_events.clear()

    # 指令处理函数
    @filter.command("回复菜单")
    async def reply_menu(self, event: AstrMessageEvent):
        """显示插件的所有可用指令"""
        menu_text = """
定时回复插件指令菜单 (v1.1.0)

添加任务：
• /添加回复 [群号] [时间] [回复内容] - 添加静态内容定时回复
• /添加动态回复 [群号] [时间] [URL] [认证头(可选)] - 添加动态内容定时回复

删除任务：
• /移除回复 [群号] [任务ID] - 删除指定群的特定定时回复
• /清空回复 [群号] - 清空指定群的所有定时回复

任务管理：
• /开启自动回复 - 启动每日定时回复
• /关闭自动回复 - 停止每日定时回复

状态与查看：
• /查看回复 [群号(可选)] - 查看定时回复任务
• /回复状态 - 查看插件状态和统计
• /立即执行 [群号(可选)] - 手动触发定时回复
• /测试动态内容 [URL] [认证头] - 测试动态内容获取功能，预览获取到的内容
        """
        yield event.chain_result([Plain(menu_text)])

    @filter.command("添加回复")
    async def add_scheduled_reply(self, event: AstrMessageEvent, group_id: str, time_str: str, *, content: str):
        """为指定群添加静态内容定时回复"""
        await self._initialized.wait()
        group_id = group_id.strip()
        time_str = time_str.strip()
        content = content.strip()
        
        if not group_id.isdigit():
            yield event.chain_result([Plain("群号格式不正确，应为纯数字。")])
            return
        
        try:
            self._parse_time(time_str)  # 验证时间格式
        except ValueError as e:
            yield event.chain_result([Plain(str(e))])
            return
        
        task_id = f"task_{int(datetime.now().timestamp())}_{random.randint(1000, 9999)}"
        new_task = {
            "task_id": task_id,
            "time": time_str,
            "content": content,
            "content_type": "static",
            "created_time": datetime.now().isoformat()
        }
        
        if group_id not in self.scheduled_tasks:
            self.scheduled_tasks[group_id] = []
        
        self.scheduled_tasks[group_id].append(new_task)
        
        if await self._save_data():
            if self.is_active:
                await self._start_task_for_group(group_id)
            yield event.chain_result([Plain(f"添加成功！\n群聊 {group_id} 的定时回复已添加\n时间: {time_str}\n内容: {content}\n任务ID: {task_id}")])
        else:
            yield event.chain_result([Plain("添加失败，保存数据时发生错误。")])

    @filter.command("添加动态回复")
    async def add_dynamic_reply(self, event: AstrMessageEvent, group_id: str, time_str: str, url: str, auth_header: str = ""):
        """为指定群添加动态内容定时回复"""
        await self._initialized.wait()
        group_id = group_id.strip()
        time_str = time_str.strip()
        url = url.strip()
        
        if not group_id.isdigit():
            yield event.chain_result([Plain("群号格式不正确，应为纯数字。")])
            return
        
        if not url.startswith(('http://', 'https://')):
            yield event.chain_result([Plain("URL格式不正确，应以 http:// 或 https:// 开头")])
            return
        
        try:
            self._parse_time(time_str)
        except ValueError as e:
            yield event.chain_result([Plain(str(e))])
            return
        
        task_id = f"dynamic_{int(datetime.now().timestamp())}_{random.randint(1000, 9999)}"
        new_task = {
            "task_id": task_id,
            "time": time_str,
            "content": url,
            "content_type": "dynamic",
            "auth_header": auth_header,
            "created_time": datetime.now().isoformat()
        }
        
        if group_id not in self.scheduled_tasks:
            self.scheduled_tasks[group_id] = []
        
        self.scheduled_tasks[group_id].append(new_task)
        
        if await self._save_data():
            if self.is_active:
                await self._start_task_for_group(group_id)
            yield event.chain_result([Plain(f"动态回复添加成功\n群聊: {group_id}\n时间: {time_str}\nURL: {url}\n任务ID: {task_id}")])
        else:
            yield event.chain_result([Plain("添加失败，保存数据时发生错误。")])

    @filter.command("移除回复")
    async def remove_scheduled_reply(self, event: AstrMessageEvent, group_id: str, task_id: str):
        """删除指定群的特定定时回复"""
        await self._initialized.wait()
        group_id = group_id.strip()
        task_id = task_id.strip()
        
        if group_id not in self.scheduled_tasks:
            yield event.chain_result([Plain(f"群 {group_id} 没有配置任何定时回复任务。")])
            return
        
        original_count = len(self.scheduled_tasks[group_id])
        self.scheduled_tasks[group_id] = [
            task for task in self.scheduled_tasks[group_id] 
            if task.get("task_id") != task_id
        ]
        
        if len(self.scheduled_tasks[group_id]) < original_count:
            await self._save_data()
            # 重启该群的任务
            if self.is_active:
                await self._start_task_for_group(group_id)
            yield event.chain_result([Plain(f"已成功移除任务 {task_id}")])
        else:
            yield event.chain_result([Plain("未找到指定的任务ID")])

    @filter.command("清空回复")
    async def clear_scheduled_replies(self, event: AstrMessageEvent, group_id: str):
        """清空指定群的所有定时回复"""
        await self._initialized.wait()
        group_id = group_id.strip()
        
        if group_id in self.scheduled_tasks and self.scheduled_tasks[group_id]:
            task_count = len(self.scheduled_tasks[group_id])
            self.scheduled_tasks[group_id] = []
            await self._save_data()
            
            # 停止该群的任务
            if group_id in self._stop_events:
                self._stop_events[group_id].set()
                if f"group_{group_id}" in self.tasks:
                    self.tasks[f"group_{group_id}"].cancel()
                    del self.tasks[f"group_{group_id}"]
            
            yield event.chain_result([Plain(f"已清空群 {group_id} 的所有 {task_count} 个定时回复任务")])
        else:
            yield event.chain_result([Plain(f"群 {group_id} 没有配置任何定时回复任务。")])

    @filter.command("查看回复")
    async def view_scheduled_replies(self, event: AstrMessageEvent, group_id: str = ""):
        """查看定时回复任务"""
        await self._initialized.wait()
        
        if group_id:
            # 查看特定群的任务
            group_id = group_id.strip()
            if group_id not in self.scheduled_tasks or not self.scheduled_tasks[group_id]:
                yield event.chain_result([Plain(f"群 {group_id} 没有配置任何定时回复任务。")])
                return
            
            reply_list = [f"群 {group_id} 的定时回复任务 ({len(self.scheduled_tasks[group_id])} 个):"]
            for i, task in enumerate(self.scheduled_tasks[group_id], 1):
                task_type = "动态" if task.get("content_type") == "dynamic" else "静态"
                content_preview = task["content"][:50] + "..." if len(task["content"]) > 50 else task["content"]
                reply_list.append(
                    f"\n{i}. 时间: {task['time']} | 类型: {task_type}\n"
                    f"   内容: {content_preview}\n"
                    f"   任务ID: {task.get('task_id', 'N/A')}"
                )
            
            yield event.chain_result([Plain("\n".join(reply_list))])
        else:
            # 查看所有群的任务
            if not self.scheduled_tasks:
                yield event.chain_result([Plain("当前没有配置任何定时回复任务。")])
                return
            
            total_tasks = sum(len(tasks) for tasks in self.scheduled_tasks.values())
            reply_list = [f"所有定时回复任务 (共 {total_tasks} 个任务):"]
            
            for group_id, tasks in self.scheduled_tasks.items():
                if tasks:
                    reply_list.append(f"\n群 {group_id} ({len(tasks)} 个任务):")
                    for i, task in enumerate(tasks, 1):
                        task_type = "动态" if task.get("content_type") == "dynamic" else "静态"
                        reply_list.append(f"  {i}. {task['time']} | {task_type} | ID: {task.get('task_id', 'N/A')}")
            
            yield event.chain_result([Plain("\n".join(reply_list))])

    @filter.command("回复状态")
    async def reply_status(self, event: AstrMessageEvent):
        """查看插件状态和统计"""
        await self._initialized.wait()
        status = "自动回复已开启" if self.is_active else "自动回复已停止"
        
        # 计算任务统计
        total_groups = len(self.scheduled_tasks)
        total_tasks = sum(len(tasks) for tasks in self.scheduled_tasks.values())
        static_tasks = 0
        dynamic_tasks = 0
        
        for tasks in self.scheduled_tasks.values():
            for task in tasks:
                if task.get("content_type") == "dynamic":
                    dynamic_tasks += 1
                else:
                    static_tasks += 1
        
        stats = self.reply_statistics
        stats_msg = (
            f"任务统计:\n"
            f"群组数量: {total_groups}\n"
            f"总任务数: {total_tasks}\n"
            f"静态任务: {static_tasks}\n"
            f"动态任务: {dynamic_tasks}\n"
            f"执行统计:\n"
            f"总计: {stats['total_replies']}\n"
            f"成功: {stats['success_count']}\n"
            f"失败: {stats['fail_count']}"
        )
        
        if stats['last_reply_time']:
            last_time = datetime.fromisoformat(stats['last_reply_time'])
            stats_msg += f"\n上次执行: {last_time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        # 显示下一个即将执行的任务
        next_task_info = "暂无即将执行的任务"
        if self.is_active and total_tasks > 0:
            soonest_time = None
            soonest_task = None
            soonest_group = None
            
            for group_id, tasks in self.scheduled_tasks.items():
                for task in tasks:
                    next_time = self._get_next_run_time(task["time"])
                    if soonest_time is None or next_time < soonest_time:
                        soonest_time = next_time
                        soonest_task = task
                        soonest_group = group_id
            
            if soonest_time:
                next_task_info = f"下一个任务: {soonest_time.strftime('%m-%d %H:%M')}\n群组: {soonest_group}\n时间: {soonest_task['time']}"
        
        message = (
            f"{status}\n"
            f"随机时间: {'开启' if self.config.get('random', False) else '关闭'}\n"
            f"{next_task_info}\n"
            f"{stats_msg}"
        )
        yield event.chain_result([Plain(message)])

    @filter.command("立即执行")
    async def manual_execute_replies(self, event: AstrMessageEvent, group_id: str = ""):
        """手动触发定时回复任务"""
        await self._initialized.wait()
        
        if group_id:
            # 执行指定群的任务
            group_id = group_id.strip()
            if group_id not in self.scheduled_tasks or not self.scheduled_tasks[group_id]:
                yield event.chain_result([Plain(f"群 {group_id} 没有配置任何定时回复任务。")])
                return
            
            yield event.chain_result([Plain(f"正在手动执行群 {group_id} 的定时回复任务...")])
            tasks = self.scheduled_tasks[group_id]
            results = await asyncio.gather(*[self._send_scheduled_reply(group_id, task) for task in tasks])
            
            success_count = sum(1 for r in results if r["success"])
            fail_count = len(results) - success_count
            
            # 更新统计
            self.reply_statistics["total_replies"] += len(results)
            self.reply_statistics["success_count"] += success_count
            self.reply_statistics["fail_count"] += fail_count
            self.reply_statistics["last_reply_time"] = datetime.now().isoformat()
            await self._save_data()
            
            result_summary = f"手动执行完成\n群组: {group_id}\n任务数: {len(tasks)}\n成功: {success_count}\n失败: {fail_count}"
        else:
            # 执行所有群的任务
            if not self.scheduled_tasks:
                yield event.chain_result([Plain("当前没有配置任何定时回复任务。")])
                return
            
            yield event.chain_result([Plain("正在手动执行所有定时回复任务...")])
            
            all_tasks = []
            for gid, tasks in self.scheduled_tasks.items():
                for task in tasks:
                    all_tasks.append((gid, task))
            
            if not all_tasks:
                yield event.chain_result([Plain("当前没有配置任何定时回复任务。")])
                return
            
            results = await asyncio.gather(*[self._send_scheduled_reply(gid, task) for gid, task in all_tasks])
            
            success_count = sum(1 for r in results if r["success"])
            fail_count = len(results) - success_count
            
            # 更新统计
            self.reply_statistics["total_replies"] += len(results)
            self.reply_statistics["success_count"] += success_count
            self.reply_statistics["fail_count"] += fail_count
            self.reply_statistics["last_reply_time"] = datetime.now().isoformat()
            await self._save_data()
            
            result_summary = f"手动执行完成\n总群组: {len(self.scheduled_tasks)}\n总任务: {len(all_tasks)}\n成功: {success_count}\n失败: {fail_count}"
        
        yield event.chain_result([Plain(result_summary)])

    @filter.command("开启自动回复")
    async def start_auto_reply(self, event: AstrMessageEvent):
        """开启自动回复功能"""
        await self._initialized.wait()
        if self.is_active:
            yield event.chain_result([Plain("自动回复任务已经在运行中。")])
            return
            
        self.is_active = True
        self.config["enable_auto_reply"] = True
        self.config.save_config()
        await self._start_all_tasks()
        yield event.chain_result([Plain("自动回复已开启，所有定时任务已启动。")])

    @filter.command("关闭自动回复")
    async def stop_auto_reply(self, event: AstrMessageEvent):
        """关闭自动回复功能"""
        await self._initialized.wait()
        if not self.is_active:
            yield event.chain_result([Plain("自动回复任务并未运行。")])
            return
            
        self.is_active = False
        self.config["enable_auto_reply"] = False
        self.config.save_config()
        await self._stop_all_tasks()
        yield event.chain_result([Plain("自动回复已停止，所有定时任务已取消。")])

    @filter.command("测试动态内容")
    async def test_dynamic_content(self, event: AstrMessageEvent, url: str, auth_header: str = ""):
        """测试动态内容获取"""
        await self._initialized.wait()
        
        if not url.startswith(('http://', 'https://')):
            yield event.chain_result([Plain("URL格式不正确，应以 http:// 或 https:// 开头")])
            return
        
        yield event.chain_result([Plain("正在测试动态内容获取...")])
        
        try:
            # 模拟一个任务来测试
            test_task = {
                "content": url,
                "content_type": "dynamic",
                "auth_header": auth_header
            }
            
            content = await self._get_task_content(test_task)
            preview = content[:200] + "..." if len(content) > 200 else content
            
            yield event.chain_result([Plain(f"测试成功 获取到的内容：\n{preview}")])
        except Exception as e:
            yield event.chain_result([Plain(f"测试失败：{str(e)}")])


    async def terminate(self):
        """插件终止时执行清理"""
        await self._stop_all_tasks()
        if self.http_session:
            await self.http_session.close()
        logger.info("定时回复插件已终止。")





