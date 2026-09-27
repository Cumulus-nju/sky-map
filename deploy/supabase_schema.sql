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
