# 公开版配置与发布检查

本仓库提供平台源码、测试、示例配置和使用文档，不包含原部署的账号、内网服务、模型权重、运行数据或业务评测记录。

公开名称为 Orbitree。现有目录、命令和配置标识符保留原名；清理旧提示词的兼容代码也保留历史品牌字面量，以识别既有会话，而不是生成新的平台提示词。

## 配置示例

- `alice`、`bob` 和 `demo-*` 是虚构用户与任务。
- `*.example.com` 是保留示例域名；`192.0.2.0/24`、`198.51.100.0/24` 和 `203.0.113.0/24` 是文档示例网段，不是可用服务。
- `/srv/research-ops` 和 `/data` 是示例目录。安装时应改用自己的目录，或使用项目相对路径。
- 外部 AgentOS、LTP、Goblin、Octo、消息服务和 OneAPI 集成保留代码，但不会附带服务或访问权限。使用这些功能前，应配置自己的接口和凭据；不需要的集成保持关闭。
- 公开任务池中的应用与 PPT 内容是合成样例，不是历史实验结果。真实运行需要自行提供数据、输入素材和验证器。

界面截图由 `research-ops/scripts/capture-public-screenshots.mjs` 使用虚构数据生成，不连接实际后端。背景图片的来源与许可见 `research-ops/web/public/themes/README.md`。

## 提交前检查

在仓库根目录执行：

```bash
python3 scripts/check-public-release.py
python3 scripts/check-public-release.py --all-history
gitleaks git --redact --log-opts="--all"
git diff --cached --check
```

内部用户名、特有任务名和组织标识应另存为不提交的逐行词表。使用 `--forbidden-file /private/deny-terms.txt` 检查时，程序只报告文件位置和类别，不输出匹配内容。

这些检查不能代替截图人工复核，也不能证明组织代码、第三方数据或素材已获得发布授权。新增业务数据、图片和外部依赖时，仍应检查内容与许可。

## 历史与运行系统

公开版使用不含原私有提交的独立历史。旧私有备份只能留在受控目录，不能作为 tag、branch、bundle、Release 附件或备份压缩包上传。

生产部署、运行中的会话和本地凭据不随公开版改动。复制公开版替换现有部署时，应先单独备份自己的配置与数据。
