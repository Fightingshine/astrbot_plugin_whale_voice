# 从0.2.x迁移专属音色

0.2.x可能把实际音色引用写在`core.py`的`VOICE_ALIASES`字典中。0.3.0开源源码不包含这些引用，迁移后改从**插件私有数据目录**的`voice-aliases.json`读取。

## 更新前

备份旧插件源码、`astrbot_plugin_whale_voice_config.json`和整个插件私有数据目录。不要将这些备份提交到GitHub。

保留旧版`core.py`作为迁移输入。脚本只使用AST解析字典，不导入或执行旧源码。

## 先检查，再应用

以下路径为占位符，请替换成你自己的私有备份与数据目录：

```powershell
python scripts/migrate_voice_config.py --legacy-source "<旧源码备份>/core.py" --destination "<插件私有数据目录>/voice-aliases.json"
python scripts/migrate_voice_config.py --legacy-source "<旧源码备份>/core.py" --destination "<插件私有数据目录>/voice-aliases.json" --apply
```

第一次命令默认只检查，不写入。确认后加`--apply`。已有同名映射与旧版不一致时会停止，不能盲目覆盖。输出只显示数量与状态，不回显音色值。

如果配置中仍选择旧别名，例如`dafeiyu`，保留这个值；迁移后的私有文件提供对应实际引用。若直接使用完整音色URI，也可以保留原配置，不需要把URI搬进源码。

首次安装者可直接选择内置音色。自定义单个音色可在后台填写`custom_voice_uri`，并把`voice`设为`custom`。这只保存到服务器配置，不应提交到仓库。

## 核验

迁移前后解析结果应一致；仅比较哈希或布尔结果，不在公开日志打印引用。确认后重载此插件，不重载其他插件、不重启容器。先检查状态，再由管理员决定是否发起真实试听。

当前曲库原音目录不用搬迁；未带索引的旧歌曲保持原先的可点播状态，新上传仍默认草稿。回滚时恢复旧源码与配置备份，保留原音频；不要为了回滚删除整棵数据目录。
