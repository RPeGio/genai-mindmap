"""数据库异常名。

各存储模块原来是 `import sqlite3` 然后 `except sqlite3.Error` / `sqlite3.IntegrityError`。
换成 PG 之后这些名字依然要读得懂，所以这里给出一组**同名不同源**的别名：

    from app.db.errors import DatabaseError, IntegrityError, OperationalError

这样调用点只换 import，`except` 结构一个字不动。**不要**在这里重新定义异常类再包
一层——把 psycopg 的异常吞掉换成自己的类，会让"到底是唯一键冲突还是连接断了"
这个判断从 `isinstance` 退化成字符串匹配，而那正是旧代码里 `_ensure_columns` 靠
`"duplicate column name" not in str(exc)` 判类型的那套坏做法。这里只是别名。
"""
from __future__ import annotations

import psycopg
from psycopg import errors as pg_errors

#: 所有数据库错误的基类。`except DatabaseError` 就是原来的 `except sqlite3.Error`。
DatabaseError = psycopg.Error

#: 连接层错误（连不上、连接被关、超时）。**降级路径要区分它**：配额表读不到时
#: `quota.consume` 选择"放行"，而"SQL 写错了"不该被当成"数据库暂时不可用"放行。
OperationalError = psycopg.OperationalError

#: 完整性约束冲突（旧代码按 `sqlite3.IntegrityError` 捕获）。
IntegrityError = psycopg.IntegrityError

#: SQL 语法/参数错误。旧代码里的 `sqlite3.ProgrammingError` 对应它。
ProgrammingError = psycopg.ProgrammingError

#: 唯一键冲突。PG 把它归到 IntegrityError 的子类里，但**具体到唯一约束**才有意义：
#: `library.ingest_one` 靠它判断"这份材料已经入过库"，而外键冲突也是 IntegrityError
#: ——用基类捕会把"引用了一条不存在的来源"当成"重复入库"静默跳过。
UniqueViolation = pg_errors.UniqueViolation

#: 事务被上一条失败的语句打成 aborted 状态。这个异常是 SQLite 没有、PG 特有的：
#: 在 SQLite 上"这条语句失败、下一条继续"是正常用法，在 PG 上必须靠 SAVEPOINT 才能
#: 恢复（见 `conn.py`）。留这个名字是为了让测试能直白地断言"这个形状不该出现"。
InFailedSqlTransaction = pg_errors.InFailedSqlTransaction

#: 表/列不存在。旧代码里对应 `sqlite3.OperationalError: no such column`，
#: 那一类判据在 PG 下要按 SQLSTATE 认，不能按字符串认。
UndefinedTable = pg_errors.UndefinedTable
UndefinedColumn = pg_errors.UndefinedColumn

__all__ = [
    "DatabaseError",
    "InFailedSqlTransaction",
    "IntegrityError",
    "OperationalError",
    "ProgrammingError",
    "UndefinedColumn",
    "UndefinedTable",
    "UniqueViolation",
]
