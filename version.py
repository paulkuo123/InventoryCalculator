"""
版本管理與自動更新檢查模組
支援兩種使用者類型：
  1. 開發者（有 .git 目錄）→ 檢查 Git 遠端更新
  2. 一般使用者（只有執行檔）→ 檢查 GitHub Releases
"""
import os
import sys
import subprocess
import requests

# ===== 版本號（每次發布新版時修改這裡） =====
CURRENT_VERSION = "1.0.0"
# ===========================================

# GitHub 倉庫資訊（⚠️ 請替換成你的實際帳號/專案名）
# 例如：GITHUB_OWNER = "myusername", GITHUB_REPO = "InventoryCalculater"
GITHUB_OWNER = "paulkuo123"  # ← 改成你的 GitHub 帳號
GITHUB_REPO = "InventoryCalculater"    # ← 改成你的 Repo 名稱


def check_for_updates():
    """
    統一更新檢查：自動判斷使用者類型
    - 開發者：檢查 Git 遠端有沒有新 commit
    - 一般使用者：檢查 GitHub Releases 最新版本
    """
    print("\n" + "=" * 50)
    print(f"📦 目前版本：v{CURRENT_VERSION}")
    print("=" * 50)

    # 判斷是否為開發者環境（有 .git 目錄）
    is_developer = os.path.exists(".git") and not getattr(sys, "frozen", False)

    if is_developer:
        _check_git_updates()
    else:
        _check_github_releases()


def _check_git_updates():
    """開發者模式：檢查 Git 遠端更新"""
    try:
        # 先 fetch 遠端資訊（不下載）
        subprocess.run(
            ["git", "fetch", "--quiet"],
            capture_output=True,
            timeout=10
        )

        # 取得本地 HEAD commit
        local = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()

        # 嘗試多個可能的遠端分支名稱
        remote_branches = ["origin/main", "origin/master", "origin/develop"]
        remote = None

        for branch in remote_branches:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                remote = result.stdout.strip()
                break

        if remote is None:
            print("⚠️  無法找到遠端分支，跳過更新檢查")
            return

        if local != remote:
            # 取得遠端最新的 commit message
            log = subprocess.run(
                ["git", "log", remote, "-1", "--oneline"],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()

            print("🔄 發現程式碼更新！")
            print(f"   遠端最新：{log}")
            print("   請執行：git pull 更新原始碼")
        else:
            print("✅ 程式碼已是最新版本")

    except subprocess.TimeoutExpired:
        print("⚠️  Git 檢查超時，跳過更新檢查")
    except FileNotFoundError:
        print("⚠️  未安裝 Git，跳過更新檢查")
    except Exception as e:
        print(f"⚠️  Git 檢查失敗：{e}")


def _check_github_releases():
    """一般使用者模式：檢查 GitHub Releases 最新版本"""
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        response = requests.get(api_url, timeout=10)

        if response.status_code != 200:
            print("⚠️  無法取得 GitHub Releases 資訊")
            return

        data = response.json()
        latest_version = data.get("tag_name", "").lstrip("v")
        release_url = data.get("html_url", "")

        if not latest_version:
            print("⚠️  尚未發布任何 Release")
            return

        if latest_version != CURRENT_VERSION:
            print(f"🔄 發現新版本：v{latest_version}（目前是 v{CURRENT_VERSION}）")
            if release_url:
                print(f"   請到以下網址下載執行檔：")
                print(f"   {release_url}")
        else:
            print("✅ 已是最新版本")

    except requests.exceptions.Timeout:
        print("⚠️  GitHub API 連線超時")
    except requests.exceptions.RequestException as e:
        print(f"⚠️  GitHub API 請求失敗：{e}")
    except Exception as e:
        print(f"⚠️  檢查更新失敗：{e}")


if __name__ == "__main__":
    # 直接執行此檔案時測試
    check_for_updates()
