# 版本、Release 与变更日志流程

## 固定约定

- `pyproject.toml` 的 `[project].version` 是唯一版本来源，不在 workflow 或 Release 页面重复维护版本号。
- 稳定版本使用 `vMAJOR.MINOR.PATCH` 标签，例如 `v0.1.0`。
- 版本号遵循 SemVer：破坏性变更升 MAJOR，向后兼容的功能升 MINOR，兼容的修复或安全修复升 PATCH。
- `CHANGELOG.md` 是人工维护的变更事实来源；GitHub Release 会把对应版本段落作为前置说明，并追加 GitHub 自动生成的提交/PR 说明。
- 版本变更、变更日志和发布相关文档必须通过 Pull Request 进入 `main`，不能直接在本地打一个未经过 CI 的发布标签。

## 发布前准备

1. 从最新 `main` 创建发布分支，修改 `pyproject.toml` 的 `project.version`。
2. 在 `CHANGELOG.md` 中把 `Unreleased` 下已经完成的内容整理为版本段落。版本段落必须使用下面的标题格式，并保留一个新的空 `Unreleased` 段落：

   ```markdown
   ## [0.2.0] - 2026-09-20

   ### Added（新增）

   - 新增可观察能力。

   ### Changed（变更）

   - 记录兼容性或行为变化。

   ### Fixed（修复）

   - 记录用户可感知的缺陷修复。

   ### Security（安全）

   - 记录安全修复或安全边界变化。

   ## [Unreleased]
   ```

3. 在 PR 中说明版本号、变更日志范围、兼容性影响和回滚方式。
4. 运行与改动相关的检查，至少包括：

   ```powershell
   uv lock --check
   uv run --extra dev pytest
   uv run ruff check src tests scripts/check_config_contract.py
   uv run python -m compileall -q src tests
   ```

5. 等待 `main` 的全部必需 CI 通过后合并 PR。发布标签必须建立在已合并的 `main` 提交上。

## 创建版本标签

以下命令只应在干净的本地 `main` checkout 或独立 worktree 中执行。当前仓库版本为 `0.1.0`，首次正式发布前应先在 PR 中补齐 `CHANGELOG.md` 的 `0.1.0` 条目。

```powershell
$version = "0.2.0"
git fetch origin main --tags
git switch main
git pull --ff-only origin main
if (git status --porcelain) { throw "工作区不干净，停止发布。" }
git tag -a "v$version" -m "Release v$version"
git push origin "v$version"
```

不要移动已经推送的标签，也不要使用 force-push。标签推送后，`.github/workflows/release.yml` 会自动执行以下门禁：

- 标签必须符合 `vMAJOR.MINOR.PATCH`；
- 标签版本必须与 `pyproject.toml` 一致；
- `CHANGELOG.md` 必须存在对应的非空版本段落；
- 标签提交必须属于远端 `main` 的历史；
- 校验通过后创建同名 GitHub Release。

## 验证 Release

```powershell
gh release view "v$version" --repo GUNJOTH/contract-review-agent
gh api repos/GUNJOTH/contract-review-agent/releases/latest --jq '{tag_name,name,published_at,url}'
```

Release 页面中的版本说明以 `CHANGELOG.md` 对应段落为前置内容，同时追加 GitHub 自动生成的提交和 PR 说明。若发布检查失败，先修复版本、标签或变更日志，再重新创建一个新的补丁版本；不要通过覆盖标签掩盖失败。

## 未发布变更的维护

- 日常 PR 只修改 `Unreleased` 段落，不提前写入未来版本号。
- 发布 PR 决定版本号并把 `Unreleased` 内容归档到版本段落。
- 安全修复放入 `Security` 分类，并在不泄露漏洞细节的前提下说明影响和修复边界。
- 没有实际用户可观察变化的纯 CI、测试或文档整理，可以在变更日志中合并描述，避免噪声条目。
