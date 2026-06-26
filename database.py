import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import asyncio
import os
import asyncpg
import aiohttp
import re
import time
import base64
import logging
import datetime
from typing import Optional
import core
from core import *



# ─────────────────────────────────────────────
#  База данных
# ─────────────────────────────────────────────
async def init_db():

    if core.db_pool is not None:
        return
    core.db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with core.db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                name       TEXT   NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, name)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id          SERIAL  PRIMARY KEY,
                playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
                title       TEXT    NOT NULL,
                uri         TEXT    NOT NULL,
                duration    INTEGER NOT NULL,
                position    INTEGER NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS birthdays (
                user_id      BIGINT PRIMARY KEY,
                birth_day    INTEGER NOT NULL,
                birth_month  INTEGER NOT NULL,
                birthday_song TEXT DEFAULT 'Happy Birthday instrumental'
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                guild_id      BIGINT PRIMARY KEY,
                tracks_played INTEGER DEFAULT 0,
                total_ms      BIGINT  DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id           BIGINT PRIMARY KEY,
                dj_role_id         BIGINT  DEFAULT NULL,
                vote_skip_enabled  BOOLEAN DEFAULT FALSE,
                vote_skip_percent  INTEGER DEFAULT 50,
                track_limit        INTEGER DEFAULT 0,
                fair_queue         BOOLEAN DEFAULT FALSE
            )
        """)
        # Миграции (безопасно добавляют колонки если их не было)
        for migration in [
            "ALTER TABLE birthdays ADD COLUMN IF NOT EXISTS "
            "birthday_song TEXT DEFAULT 'Happy Birthday instrumental'",
            "ALTER TABLE server_settings ADD COLUMN IF NOT EXISTS "
            "fair_queue BOOLEAN DEFAULT FALSE",
        ]:
            try:
                await conn.execute(migration)
            except Exception as e:
                log.debug("Migration skipped: %s", e)


async def db_get_settings(guild_id: int) -> dict:
    async with core.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM server_settings WHERE guild_id=$1", guild_id
        )
        if row:
            return dict(row)
        return {
            "guild_id": guild_id,
            "dj_role_id": None,
            "vote_skip_enabled": False,
            "vote_skip_percent": 50,
            "track_limit": 0,
            "fair_queue": False,
        }


async def db_save_settings(guild_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kwargs))
    values = list(kwargs.values())
    async with core.db_pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO server_settings (guild_id, {', '.join(kwargs.keys())}) "
            f"VALUES ($1, {', '.join(f'${i+2}' for i in range(len(kwargs)))}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {fields}",
            guild_id, *values
        )


async def db_create_playlist(user_id: int, name: str) -> Optional[int]:
    try:
        async with core.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO playlists (user_id, name) VALUES ($1, $2) RETURNING id",
                user_id, name
            )
            return row["id"]
    except asyncpg.UniqueViolationError:
        return None


async def db_get_playlist(user_id: int, name: str) -> Optional[dict]:
    async with core.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM playlists WHERE user_id=$1 AND name=$2", user_id, name
        )
        return dict(row) if row else None


async def db_get_user_playlists(user_id: int) -> list:
    async with core.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id, p.name, COUNT(t.id) AS track_count "
            "FROM playlists p LEFT JOIN playlist_tracks t ON p.id=t.playlist_id "
            "WHERE p.user_id=$1 GROUP BY p.id ORDER BY p.created_at",
            user_id
        )
        return [dict(r) for r in rows]


async def db_delete_playlist(user_id: int, name: str) -> bool:
    async with core.db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM playlists WHERE user_id=$1 AND name=$2", user_id, name
        )
        return result != "DELETE 0"


async def db_add_track(playlist_id: int, title: str, uri: str, duration: int):
    safe_duration = max(0, min(duration, MAX_INT32))
    async with core.db_pool.acquire() as conn:
        pos = await conn.fetchval(
            "SELECT COALESCE(MAX(position),0)+1 FROM playlist_tracks WHERE playlist_id=$1",
            playlist_id
        )
        await conn.execute(
            "INSERT INTO playlist_tracks (playlist_id,title,uri,duration,position) "
            "VALUES ($1,$2,$3,$4,$5)",
            playlist_id, title, uri, safe_duration, pos
        )


async def db_get_tracks(playlist_id: int) -> list:
    async with core.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM playlist_tracks WHERE playlist_id=$1 ORDER BY position",
            playlist_id
        )
        return [dict(r) for r in rows]


async def db_set_birthday(user_id: int, day: int, month: int):
    async with core.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO birthdays (user_id, birth_day, birth_month) VALUES ($1,$2,$3) "
            "ON CONFLICT (user_id) DO UPDATE SET birth_day=$2, birth_month=$3",
            user_id, day, month
        )


async def db_get_birthday(user_id: int) -> Optional[dict]:
    async with core.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM birthdays WHERE user_id=$1", user_id)
        return dict(row) if row else None


async def db_increment_stats(guild_id: int, duration_ms: int):
    safe_duration = max(0, min(duration_ms, MAX_INT32))
    async with core.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO stats (guild_id, tracks_played, total_ms) VALUES ($1, 1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET "
            "tracks_played = stats.tracks_played + 1, "
            "total_ms = stats.total_ms + $2",
            guild_id, safe_duration
        )


async def db_get_stats(guild_id: int) -> Optional[dict]:
    async with core.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM stats WHERE guild_id=$1", guild_id)
        return dict(row) if row else None

