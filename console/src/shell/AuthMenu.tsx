import { useState } from "react";

import { Button, ErrorText, fmtTime } from "../components/ui";
import { useEdge } from "../edge";
import { errMsg } from "../types";
import { Dropdown } from "./Dropdown";
import { Chip } from "./controls";

/** 顶栏飞书登录：OAuth 用整页跳转，回调后 Cookie 自动恢复当前用户。 */
export function AuthMenu() {
    const { api, authStatus, auth, refreshAuth } = useEdge();
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);

    if (!api || !authStatus?.enabled) return null;

    const login = () => {
        window.location.assign(api.authLoginUrl(window.location.pathname));
    };
    const logout = async () => {
        setBusy(true);
        setError(null);
        try {
            await api.authLogout();
            await refreshAuth();
        } catch (e) {
            setError(errMsg(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Dropdown
            title="飞书采集员身份"
            panelClass="w-72"
            label={<Chip tone={auth ? "green" : "amber"} label="飞书" value={auth?.user.display_name ?? "未登录"} />}
        >
            <div className="space-y-2">
                {auth ? (
                    <>
                        <div className="space-y-1 rounded border border-line bg-panel2 p-2 text-[11px]">
                            <Row k="姓名" v={auth.user.display_name} />
                            <Row k="主体" v={auth.user.subject} />
                            <Row k="登录时间" v={fmtTime(auth.authenticated_at)} />
                        </div>
                        <Button
                            variant="danger"
                            size="xs"
                            disabled={busy}
                            className="w-full"
                            onClick={() => void logout()}
                        >
                            退出登录
                        </Button>
                    </>
                ) : (
                    <>
                        <p className="text-[11px] text-muted">登录后，采集员身份会由后端写入元信息并绑定租约。</p>
                        <Button variant="primary" size="xs" className="w-full" onClick={login}>
                            使用飞书登录
                        </Button>
                    </>
                )}
                <ErrorText message={error} />
            </div>
        </Dropdown>
    );
}

function Row({ k, v }: { k: string; v: string }) {
    return (
        <div className="flex items-baseline justify-between gap-2">
            <span className="text-[10px] text-faint">{k}</span>
            <span className="min-w-0 truncate font-mono text-[11px] text-ink" title={v}>
                {v}
            </span>
        </div>
    );
}
