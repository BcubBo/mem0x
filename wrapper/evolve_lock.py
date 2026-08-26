"""后台任务共享锁：防止 evolve_mem 和 consolidation 并发执行。"""
import threading

# 全局锁：evolve_mem 和 consolidation 入口处竞争
background_tasks_lock = threading.Lock()
