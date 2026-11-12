import { useState } from "react";

import { Button, ErrorText } from "../components/ui";
import { useEdge } from "../edge";
import { errMsg } from "../types";
import { Chip } from "./controls";
import { Dropdown } from "./Dropdown";

/** 顶栏飞书身份：未登录直接跳 OAuth；已登录点击用户名可切换或退出账号。 */
export function AuthMenu() {
    const { api, authStatus, auth, refreshAuth } = useEdge();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    if (!api || !authStatus?.enabled) return null;

    if (auth) {
        const logout = async (switchAccount: boolean) => {
            if (busy) return;
            setBusy(true);
            setError(null);
            try {
                await api.authLogout();
                if (switchAccount) {
                    window.location.assign(api.authLoginUrl(window.location.pathname));
                    return;
                }
                await refreshAuth();
            } catch (cause) {
                setError(errMsg(cause));
            } finally {
                setBusy(false);
            }
        };

        return (
            <Dropdown
                title="飞书账号"
                panelClass="w-56"
                label={<Chip tone="green" label="飞书" value={auth.user.display_name} title={auth.user.subject} />}
            >
                <div className="space-y-2">
                    <p className="truncate font-mono text-[10px] text-faint" title={auth.user.subject}>
                        {auth.user.subject}
                    </p>
                    <div className="grid grid-cols-2 gap-1.5">
                        <Button size="xs" disabled={busy} onClick={() => void logout(true)}>
                            切换账号
                        </Button>
                        <Button variant="danger" size="xs" disabled={busy} onClick={() => void logout(false)}>
                            退出登录
                        </Button>
                    </div>
                    <ErrorText message={error} />
                </div>
            </Dropdown>
        );
    }

    return (
        <Button
            variant="primary"
            size="xs"
            title="使用飞书 OAuth 登录"
            onClick={() => window.location.assign(api.authLoginUrl(window.location.pathname))}
        >
            登录飞书
        </Button>
    );
}
