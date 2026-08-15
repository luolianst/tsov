-- tsov_import.lua
-- Reaper ReaScript (Lua): 把 tsov 的 MIDI 产物导入 Reaper 并挂 ReaSynth 播放（最小闭环）
-- 运行方式：Actions -> Show action list -> ReaScript: Load -> 选本文件 -> Run
-- 操作日志：output\reaper-script-log.txt（远程验证用）
-- v2: 修复 InsertMedia 语义——mode=1 自动建轨，不再预建轨道（2026-08-15 诊断实测）
-- 实测结论：InsertMedia(mode=1) = 新建轨道 + 插入 MIDI（轨道名=文件名）；预建轨道会被无视

local PROJECT_ROOT = "<repo>"
local OUTPUT_DIR   = PROJECT_ROOT .. "/output"
local LOG_PATH     = OUTPUT_DIR .. "/reaper-script-log.txt"
local INITIAL_DIR  = OUTPUT_DIR

local log_f = io.open(LOG_PATH, "a")

local function log(msg)
  local line = "[" .. os.date("%Y-%m-%d %H:%M:%S") .. "] " .. msg
  if log_f then
    log_f:write(line .. "\n")
    log_f:flush()
  end
  reaper.ShowConsoleMsg(line .. "\n")
end

-- ============ 简易 JSON 解析器（对象/数组/字符串/数字/布尔/null） ============
local function json_parse(s)
  local pos = 1
  local function skip_ws()
    while pos <= #s and s:sub(pos, pos):match("%s") do pos = pos + 1 end
  end
  local function parse_value()
    skip_ws()
    local c = s:sub(pos, pos)
    if c == "{" then
      pos = pos + 1
      local obj = {}
      skip_ws()
      if s:sub(pos, pos) == "}" then pos = pos + 1 return obj end
      while true do
        skip_ws()
        local k = parse_value()
        skip_ws()
        if s:sub(pos, pos) ~= ":" then error("expected :") end
        pos = pos + 1
        local v = parse_value()
        obj[k] = v
        skip_ws()
        local sep = s:sub(pos, pos)
        if sep == "," then pos = pos + 1
        elseif sep == "}" then pos = pos + 1 break
        else error("expected , or }") end
      end
      return obj
    elseif c == "[" then
      pos = pos + 1
      local arr = {}
      skip_ws()
      if s:sub(pos, pos) == "]" then pos = pos + 1 return arr end
      while true do
        arr[#arr + 1] = parse_value()
        skip_ws()
        local sep = s:sub(pos, pos)
        if sep == "," then pos = pos + 1
        elseif sep == "]" then pos = pos + 1 break
        else error("expected , or ]") end
      end
      return arr
    elseif c == '"' then
      pos = pos + 1
      local buf = {}
      while pos <= #s do
        local ch = s:sub(pos, pos)
        if ch == "\\" then
          pos = pos + 1
          local esc = s:sub(pos, pos)
          if esc == "n" then buf[#buf + 1] = "\n"
          elseif esc == "t" then buf[#buf + 1] = "\t"
          elseif esc == "r" then buf[#buf + 1] = "\r"
          elseif esc == '"' then buf[#buf + 1] = '"'
          elseif esc == "\\" then buf[#buf + 1] = "\\"
          elseif esc == "/" then buf[#buf + 1] = "/"
          elseif esc == "u" then
            buf[#buf + 1] = "\\u" .. s:sub(pos + 1, pos + 4)
            pos = pos + 4
          else buf[#buf + 1] = esc end
          pos = pos + 1
        elseif ch == '"' then
          pos = pos + 1
          break
        else
          buf[#buf + 1] = ch
          pos = pos + 1
        end
      end
      return table.concat(buf)
    elseif c == "-" or c:match("%d") then
      local num = s:match("^-?%d+%.?%d*[eE]?[+-]?%d*", pos)
      if not num then error("bad number") end
      pos = pos + #num
      return tonumber(num)
    elseif s:sub(pos, pos + 3) == "true" then pos = pos + 4 return true
    elseif s:sub(pos, pos + 4) == "false" then pos = pos + 5 return false
    elseif s:sub(pos, pos + 3) == "null" then pos = pos + 4 return nil
    else error("unexpected char " .. c) end
  end
  local v = parse_value()
  skip_ws()
  return v
end

local function load_json_file(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a")
  f:close()
  local ok, data = pcall(json_parse, content)
  if not ok then return nil end
  return data
end

-- ============ 功能函数 ============

-- 挂 ReaSynth（名称匹配 4 候选按序试，命中哪个写日志）
local function add_rea_synth(track)
  local candidates = {
    "ReaSynth (Cockos)",
    "VST: ReaSynth (Cockos)",
    "VST3: ReaSynth (Cockos)",
    "ReaSynth",
  }
  for _, name in ipairs(candidates) do
    local ok, idx = pcall(reaper.TrackFX_AddByName, track, name, 0, -1)
    if ok and idx and idx >= 0 then
      return idx, name
    end
    log("TrackFX_AddByName(" .. name .. ") -> not found")
  end
  return -1, nil
end

-- 加分项：读 MIDI 同目录 stage-*.json 的 segments，时间线标乐句区域
local function add_phrase_markers(midi_file)
  local dir = midi_file:match("^(.*)[/\\][^/\\]+$") or "."
  local voice_json = dir .. "/stage-01-voice.json"
  local score_json = dir .. "/stage-04-score.json"
  local src = nil
  local f = io.open(voice_json, "r")
  if f then f:close() src = voice_json end
  if not src then
    f = io.open(score_json, "r")
    if f then f:close() src = score_json end
  end
  if not src then
    log("markers: no stage-*.json beside MIDI, skipped")
    return 0
  end
  local data = load_json_file(src)
  if not data then
    log("markers: parse error " .. src)
    return 0
  end
  local segs = {}
  if type(data.segments) == "table" then segs = data.segments end
  local count = 0
  for i, s in ipairs(segs) do
    if type(s) == "table" and s.start then
      local start = tonumber(s.start) or 0
      local finish = tonumber(s["end"]) or (start + 1)
      local rgnend = math.max(finish, start + 0.01)
      local ok = reaper.AddProjectMarker(0, 1, start, rgnend, "phrase-" .. i, -1)
      if ok then count = count + 1 end
    end
  end
  log("markers: added " .. count .. " phrase regions from " .. src)
  return count
end

-- 播放（transport_Play 不在绑定里，用 OnPlayButton 等效）
local function play()
  local state = reaper.GetPlayState()
  log("play state before: " .. state)
  if state == 0 or state == 2 then
    reaper.OnPlayButton()
    log("play: OnPlayButton called")
  else
    log("play: already playing/recording, skip")
  end
  local after = reaper.GetPlayState()
  log("play state after: " .. after)
  return after
end

-- ============ 主流程 ============
function main()
  log("=== tsov import ReaScript start (Lua v2) ===")
  log("lua version: " .. _VERSION)

  -- 1) 文件选择对话框（初始目录 = output\）
  local ret, filename = reaper.GetUserFileNameForRead(INITIAL_DIR, "Select tsov MIDI file", "mid")
  log("dialog ret=" .. tostring(ret) .. " filename=" .. tostring(filename))
  if ret == 0 or not ret or not filename or filename == "" then
    log("user cancelled")
    return
  end
  local f = io.open(filename, "r")
  if not f then
    log("ERROR: file not found: " .. filename)
    return
  end
  f:close()
  if not filename:lower():match("%.midi?$") then
    log("WARN: not a .mid file: " .. filename)
  end

  -- 2) InsertMedia(mode=1) = 自动新建轨道并插入 MIDI（v2 实测：预建轨道会被无视）
  local n_before = reaper.CountTracks(0)
  local ok = reaper.InsertMedia(filename, 1)
  log("InsertMedia(mode=1) ret=" .. tostring(ok))
  if not ok then
    log("ERROR: InsertMedia returned false")
    return
  end
  -- 自动建的轨道插在末尾：插入前 N 条，新轨道 = idx N
  local track = reaper.GetTrack(0, n_before)
  if not track then
    log("ERROR: cannot get new track (n_before=" .. n_before .. ")")
    return
  end
  log("track acquired at idx=" .. n_before)

  -- 3) 命名
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "tsov melody", true)
  log("track named: tsov melody")

  -- 4) 验证 MIDI item
  local n = reaper.CountTrackMediaItems(track)
  log("media items on track: " .. n)
  if n == 0 then
    log("ERROR: no media item on new track")
    return
  end

  -- 5) 挂 ReaSynth
  local fx_idx, fx_name = add_rea_synth(track)
  if fx_idx >= 0 then
    log("ReaSynth added: index=" .. fx_idx .. " name=" .. tostring(fx_name))
  else
    log("ERROR: ReaSynth NOT added (none of the candidate names matched)")
  end

  -- 6) 加分项：乐句 markers
  add_phrase_markers(filename)

  -- 7) 播放
  play()

  log("=== tsov import ReaScript done ===")
end

main()
