tsov 一键开箱包 v{VERSION} —— 使用说明
============================================

【这是什么】
  tsov（the shape of voice）——「哼一段旋律 → 拿到一首能继续编辑的歌」的开源音乐工作台。
  这个包是 Windows 一键版：Python 运行环境、AI 引擎（转录/音源/效果器）、ffmpeg 全部打包在内，
  你的电脑不需要安装任何东西。

【怎么启动】
  1. 把整个文件夹解压到任意位置（建议选个短一点、全英文的路径，比如 D:\tsov）。
  2. 双击「启动tsov.bat」。
  3. 等 10～30 秒，浏览器会自动打开工作台。
  4. 关闭那个黑色窗口 = 退出 tsov。

【第一次使用：填一个 AI Key（对话/分析功能需要）】
  启动后点右上角 ⚙ →「对话 / LLM」：
    · Key：粘贴你的 API Key（形如 sk-...，默认接 DeepSeek，官网 platform.deepseek.com 可申请）
    · 地址 / 模型：默认就是 DeepSeek 官方，不用改（也支持任意 OpenAI 兼容接口）
    · 点「测试连接」验证 → 点「保存」即可用（Key 只存在你自己电脑的这个文件夹里，不上传）
  不想用 AI？不填也能玩：导入音频、转谱（哼唱转录）、写谱、混音、导出全部可用。

【快速上手】
  ☰ 文件 → 新建工程 → 导入一段哼唱录音（或音频/MIDI）
  → 右键音频轨「转乐谱…」→ 点「▶ 运行全链」（降噪→响度→转录→量化→吸附）
  → 采纳 → 右侧对话面板里让 AI 帮你改谱/编曲，或自己动手写。
  详细功能见 docs/tsov功能表.md。

【常见问题】
  · 双击没反应 / 窗口一闪而过：多半是没完整解压——请先右键压缩包「全部解压」，再双击。
  · 出现蓝色 SmartScreen 提示：点「更多信息」→「仍要运行」（本包未做数字签名，属正常）。
  · 杀毒软件拦截：个别杀软对「自带 Python 的压缩包」敏感，加入信任即可。
  · 提示端口被占用：会自动换端口；若浏览器没跟上，以黑窗口里打印的「服务地址」为准。
  · 移动了文件夹：直接照常双击启动——启动器会自愈（自动修正内部路径）。
  · 想换电脑/拷给别人：整个文件夹拷走即可（首次启动会自检+自愈）。
  · 转录很慢：第一次转录会加载模型（几十秒）；有 NVIDIA 显卡会自动用 GPU 加速。
  · 恢复出厂：删掉整个文件夹重解压一次（你的作品在 output\ 子文件夹里，可先拷出）。

【目录速览】
  启动tsov.bat        双击这个
  bootstrap.py        启动引导/搬家修复/自检（可运行 `python-base\python.exe bootstrap.py check` 排查）
  python-base\        自带的 Python 运行时
  tsov\               程序本体（源码 + tsov\.venv 运行环境）
  vendor\             AI 引擎：FluidSynth 音源 / RMVPE 哼唱转录 / GAME 人声转录 / VST3 插件
  ffmpeg\             音频处理（ffmpeg.exe + ffprobe.exe）
  docs\               文档（功能表 / 术语表 / 引擎说明）

【许可与声明】
  tsov 本体：AGPL-3.0（LICENSE）。第三方组件（FluidSynth / FluidR3_GM 音色库 / RMVPE / GAME /
  Dexed / TAL-Chorus-LX / ffmpeg 等）版权归各自作者，许可与出处见 README 与 docs\setup-engines.md。
  本项目不提供任何 AI API Key，Key 由使用者自备。

  项目主页：https://github.com/luolianst/tsov
