-- 「天光云影」打卡点地图 —— Supabase 表结构
--
-- 用法：打开 Supabase 项目 → 左侧 SQL Editor → New query → 粘贴执行。
--
-- 设计说明：
--   * submissions：一条投稿一行，整条记录放 jsonb，字段将来加了也不用改表。
--   * photos：照片直接以 base64 存库。几百幅作品（免费层 500MB）足够，
--     好处是只需要一对 URL/KEY，不用再配对象存储。
-- 安全：开启 RLS 且不建任何策略 —— 只有带 service_role key 的后端
--       （也就是你的 Streamlit 应用）能访问，anon key 拿到也读不到数据。

-- ---------------------------------------------------------------- 投稿
create table if not exists public.submissions (
    sid         text primary key,
    data        jsonb not null,
    updated_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- 照片
create table if not exists public.photos (
    id          text primary key,
    mime        text default 'image/jpeg',
    origin      text,          -- 原图 base64
    thumb       text,          -- 缩略图 base64
    updated_at  timestamptz not null default now()
);

-- 自动维护 updated_at
create or replace function public.touch_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_submissions_touch on public.submissions;
create trigger trg_submissions_touch
    before update on public.submissions
    for each row execute function public.touch_updated_at();

drop trigger if exists trg_photos_touch on public.photos;
create trigger trg_photos_touch
    before update on public.photos
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------- 权限
alter table public.submissions enable row level security;
alter table public.photos      enable row level security;
-- 刻意不创建任何 policy：默认拒绝匿名访问，只有 service_role 能绕过 RLS。

-- 便于在后台看用量
create or replace view public.storage_usage as
select
    (select count(*) from public.submissions)                as submissions,
    (select count(*) from public.photos)                     as photos,
    pg_size_pretty(pg_total_relation_size('public.submissions')) as submissions_size,
    pg_size_pretty(pg_total_relation_size('public.photos'))      as photos_size;


-- ================================================================ 人气投票
--
-- 2026-10-06 新增。设计要点（这一段解释"为什么不是几张简单的表"）：
--
--   1. **不存明文手机号/学号**。库里只有加盐哈希（sid_h / phone_h，用于判定
--      "是不是同一个人"）和**掩码**（138****1234，供管理员人工核对争议）。
--      明文一旦进库，任何一次备份、导出、日志都是泄露。
--   2. **"最多 3 票"必须由数据库保证**。前端把按钮灰掉拦不住刷新，
--      先查后写也拦不住并发（两条请求同时读到"还差 1 票"）⇒ 全部判断
--      收进 `cast_vote()`，并在同一事务里用 `pg_advisory_xact_lock` 按 uid
--      串行化。
--   3. **学号与手机号各自唯一**。只靠 (学号,手机号) 组合去重的话，
--      (学号A,手机B) 与 (学号C,手机B) 会算成两个人 ⇒ 一个人能投 6 票。

-- ---------------------------------------------------------------- 投票人
create table if not exists public.voters (
    uid         text primary key,          -- = **学号/工号的加盐哈希**（一个学号 = 一个身份）
    phone_h     text not null unique,      -- 手机号哈希：因子之一，且全局唯一
    name_h      text not null default '',  -- 姓名哈希：因子之一（**不做唯一**，同名同姓是真实存在的）
    name        text not null default '',  -- 姓名明文：只给管理员核对用，网页上从不显示
    sid_mask    text not null default '',  -- 掩码，只为管理员核对争议用
    phone_mask  text not null default '',
    nick        text not null default '',
    created_at  timestamptz not null default now()
);

-- 重复执行本文件时的兼容：如果 voters 是更早那版（没有 name / name_h）建的，
-- 上面的 create table if not exists 会**整句跳过**，于是新列永远不存在、
-- 投票时报 404/400。所以显式补列。
alter table public.voters add column if not exists name_h text not null default '';
alter table public.voters add column if not exists name   text not null default '';

-- ---------------------------------------------------------------- 投票流水
-- 主键 (uid, work_sid) 就是"同一作品只能投一次"的硬约束 —— 不靠代码自觉。
create table if not exists public.votes (
    uid         text not null,
    work_sid    text not null,
    created_at  timestamptz not null default now(),
    primary key (uid, work_sid)
);
create index if not exists votes_work_idx on public.votes (work_sid);
create index if not exists votes_uid_idx  on public.votes (uid);

alter table public.voters enable row level security;
alter table public.votes  enable row level security;
-- 同 submissions/photos：刻意不建 policy，只有 service_role（应用后端）能访问。
-- 同学端拿不到票数就是靠这一条 —— 前端不显示只是"看不见"，
-- 这里才是"查不到"。

-- 作品是否在投票名单里：必须是 submissions 中状态为「已入围」、且非示例数据。
-- 为什么要查而不是信前端：前端传上来的作品编号是不可信输入，
-- 不查的话可以给任意编号刷票（包括不存在的编号）。
create or replace function public.vote_eligible(p_work text)
returns boolean
language sql
stable
as $$
    select exists (
        select 1 from public.submissions s
        where s.sid = p_work
          and coalesce(s.data->>'status', '') = '已入围'
          and coalesce(s.data->>'is_demo', 'false') in ('false', 'f', '0', '')
    );
$$;

-- ---------------------------------------------------------------- 投票
-- 返回值统一形状：{"ok":bool,"reason":str,"used":int,"left":int}
-- reason 取值与 vote_store.REASON_TEXT 一一对应。
--
-- 身份模型：`p_uid` 是**学号哈希**（账号），`p_phone_h` / `p_name_h` 是两个因子。
--   * 同一学号换个手机号或换个姓名 → identity_mismatch
--     （挡"借同学的手机号再投一次""随手编个名字再投一次"）
--   * 同一手机号配不同学号 → identity_conflict（靠 voters.phone_h 的 unique 约束）
--   * 姓名**不做唯一约束** —— 同名同姓是真实存在的，撞名不能算作弊。
create or replace function public.cast_vote(
    p_uid        text,
    p_phone_h    text,
    p_name_h     text,
    p_name       text,
    p_sid_mask   text,
    p_phone_mask text,
    p_nick       text,
    p_work       text,
    p_open       timestamptz,
    p_close      timestamptz,
    p_limit      int default 3
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_now      timestamptz := now();   -- 用**数据库**时间，不信客户端时钟
    v_used     int;
    v_phone_h  text;
    v_name_h   text;
begin
    if v_now < p_open then
        return jsonb_build_object('ok', false, 'reason', 'not_open', 'used', 0, 'left', 0);
    end if;
    if v_now > p_close then
        return jsonb_build_object('ok', false, 'reason', 'closed', 'used', 0, 'left', 0);
    end if;

    -- 按 uid 串行化：同一人的并发请求排队，杜绝"各自读到还差 1 票"而投出第 4 票。
    -- 事务级锁，函数返回时自动释放。
    perform pg_advisory_xact_lock(hashtext(p_uid));

    if not public.vote_eligible(p_work) then
        return jsonb_build_object('ok', false, 'reason', 'no_such_work', 'used', 0, 'left', 0);
    end if;

    select count(*) into v_used from public.votes where uid = p_uid;

    -- 先查"这幅投过没有"：最常见的误操作是连点两下，
    -- 这时回"你已经投过这幅"比回"票用完了"准确。
    if exists (select 1 from public.votes where uid = p_uid and work_sid = p_work) then
        return jsonb_build_object('ok', false, 'reason', 'duplicate',
                                  'used', v_used, 'left', greatest(0, p_limit - v_used));
    end if;

    -- 三个因子一起核对：姓名、手机号都必须与首次登记完全一致
    select phone_h, name_h into v_phone_h, v_name_h from public.voters where uid = p_uid;
    if found then
        if v_phone_h <> p_phone_h or coalesce(v_name_h, '') <> p_name_h then
            return jsonb_build_object('ok', false, 'reason', 'identity_mismatch',
                                      'used', v_used, 'left', greatest(0, p_limit - v_used));
        end if;
    elsif exists (select 1 from public.voters where phone_h = p_phone_h) then
        return jsonb_build_object('ok', false, 'reason', 'identity_conflict',
                                  'used', v_used, 'left', greatest(0, p_limit - v_used));
    end if;

    if v_used >= p_limit then
        return jsonb_build_object('ok', false, 'reason', 'limit', 'used', v_used, 'left', 0);
    end if;

    -- 上面那句"同手机号是否存在"与这句插入之间有竞态窗口（两个不同 uid、
    -- 同一个手机号），会撞 unique 约束。捕获它并翻译成 identity_conflict，
    -- 否则同学看到的是 HTTP 400 而不是一句能看懂的话。
    begin
        insert into public.voters (uid, phone_h, name_h, name, sid_mask, phone_mask, nick)
        values (p_uid, p_phone_h, p_name_h, p_name, p_sid_mask, p_phone_mask, p_nick)
        on conflict (uid) do update set nick = excluded.nick;
    exception when unique_violation then
        return jsonb_build_object('ok', false, 'reason', 'identity_conflict',
                                  'used', v_used, 'left', greatest(0, p_limit - v_used));
    end;

    insert into public.votes (uid, work_sid) values (p_uid, p_work);

    v_used := v_used + 1;
    return jsonb_build_object('ok', true, 'reason', 'ok',
                              'used', v_used, 'left', greatest(0, p_limit - v_used));
end $$;

-- ---------------------------------------------------------------- 撤回
-- 撤回逻辑抽成一个内部函数，让"同学自己撤"和"管理员删"共用同一段代码
-- （各写一遍必然有一处忘记算 left）。
create or replace function public._do_retract(p_uid text, p_work text, p_limit int)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_deleted int;
    v_used    int;
begin
    perform pg_advisory_xact_lock(hashtext(p_uid));
    delete from public.votes where uid = p_uid and work_sid = p_work;
    get diagnostics v_deleted = row_count;
    select count(*) into v_used from public.votes where uid = p_uid;
    if v_deleted = 0 then
        return jsonb_build_object('ok', false, 'reason', 'no_such_vote',
                                  'used', v_used, 'left', greatest(0, p_limit - v_used));
    end if;
    return jsonb_build_object('ok', true, 'reason', 'ok',
                              'used', v_used, 'left', greatest(0, p_limit - v_used));
end $$;

-- 同学自己撤回：**受时间窗限制**（截止后不能改票，否则可以卡点换票）。
create or replace function public.retract_vote(
    p_uid text, p_work text, p_open timestamptz, p_close timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_now timestamptz := now();
begin
    if v_now < p_open then
        return jsonb_build_object('ok', false, 'reason', 'not_open', 'used', 0, 'left', 0);
    end if;
    if v_now > p_close then
        return jsonb_build_object('ok', false, 'reason', 'closed', 'used', 0, 'left', 0);
    end if;
    return public._do_retract(p_uid, p_work, 3);
end $$;

-- 管理员删票：**不受时间窗限制**（投票结束后发现刷票，仍然要能处理）。
create or replace function public.admin_retract_vote(
    p_uid text, p_work text, p_open timestamptz, p_close timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
begin
    return public._do_retract(p_uid, p_work, 3);
end $$;

-- ---------------------------------------------------------------- 后台统计
create or replace view public.vote_tally as
select work_sid, count(*)::int as votes
from public.votes
group by work_sid
order by votes desc, work_sid asc;

-- 用量视图的**扩充版**放在最后：它要统计 voters / votes，必须等这两张表建好。
-- ⚠ 如果把它写在前面（第 53 行那里），整段 SQL 会在这里报
--   "relation public.voters does not exist" 而中断 —— 顺序不能反。
-- `create or replace view` 只允许在**末尾**加列，所以新增两列放在最后。
create or replace view public.storage_usage as
select
    (select count(*) from public.submissions)                as submissions,
    (select count(*) from public.photos)                     as photos,
    pg_size_pretty(pg_total_relation_size('public.submissions')) as submissions_size,
    pg_size_pretty(pg_total_relation_size('public.photos'))      as photos_size,
    (select count(*) from public.voters)                     as voters,
    (select count(*) from public.votes)                       as votes;
