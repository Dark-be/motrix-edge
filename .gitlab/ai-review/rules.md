# motrix-edge 的 AI review 附加规则

本仓的长期约定正文写在 `CLAUDE.md`。review 服务会自动注入它：根目录的文件总是生效，diff 触及的子目录按目录追加。本文件不重复抄写这些条目，只写 review 时额外需要遵守的判定口径。

-   违反 `CLAUDE.md` 中任一条 → 提 finding，并在正文引用对应条目的原文，不自行扩写规则。
-   规则从 MR 的 base 版本读取：本文件与 `CLAUDE.md` 的改动不作用于当前 MR，从下一个 MR 起生效。引用规则时以 base 版本为准。
