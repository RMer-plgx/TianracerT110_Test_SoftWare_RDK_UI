# tb-verify vX.Y.Z

## 主要变化

- 按用户可见行为说明本版变更，包含必要的兼容性和升级影响。

## 下载

| 文件 | 平台与用途 |
| --- | --- |
| `tb-verify-ui.exe` | Windows x86-64 图形界面 |
| `tb-verify-cli.exe` | Windows x86-64 命令行 |
| `SHA256SUMS.txt` | 附件 SHA-256 校验值 |
| `release-manifest.json` | 版本、声明的源码 SHA 和附件信息 |

## 验证与限制

- 源码提交：完整 SHA；tag 必须指向该提交。
- 构建环境与验证：列出实际执行的检查、环境及结果，区分源码、产物和实车验证。
- 限制：说明未执行的验证、签名状态和已知问题；不要把声明的源码 SHA 当作独立构建证明。

详细流程见 [RELEASE.md](../RELEASE.md)。
