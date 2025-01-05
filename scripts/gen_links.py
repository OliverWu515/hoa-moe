import asyncio
import aiohttp
import argparse
import urllib.parse
import os
import re
from datetime import datetime
from fs import filesize
from functools import lru_cache
from typing import List, Dict, Optional
import time
from aiohttp import ClientTimeout
from tenacity import retry, stop_after_attempt, wait_exponential


class GitHubAPIClient:
    def __init__(self, owner: str, repo: str, token: str):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        self.commit_cache = {}
        self.batch_size = 100  # GitHub API allows up to 100 items per page

    async def fetch_content(self, session: aiohttp.ClientSession, url: str) -> Dict:
        timeout = ClientTimeout(total=30)
        async with session.get(url, headers=self.headers, timeout=timeout) as response:
            if response.status == 200:
                return await response.json()
            raise Exception(f"Failed to fetch {url}: {response.status}")

    async def get_commits_batch(self, session: aiohttp.ClientSession, paths: List[str]) -> Dict[str, str]:
        """批量获取多个文件的提交信息"""
        results = {}
        # 构建GraphQL查询
        query = """
        query($owner: String!, $repo: String!, $paths: [String!]!) {
          repository(owner: $owner, name: $repo) {
            objects: object(expression: "HEAD") {
              ... on Tree {
                entries {
                  path
                  object {
                    ... on Blob {
                      history(first: 1) {
                        nodes {
                          committedDate
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """

        variables = {
            "owner": self.owner,
            "repo": self.repo,
            "paths": paths
        }

        url = "https://api.github.com/graphql"
        async with session.post(url, headers=self.headers, json={"query": query, "variables": variables}) as response:
            if response.status == 200:
                data = await response.json()
                entries = data.get("data", {}).get("repository", {}).get("objects", {}).get("entries", [])
                for entry in entries:
                    path = entry["path"]
                    history = entry.get("object", {}).get("history", {}).get("nodes", [])
                    if history:
                        date = history[0]["committedDate"]
                        dt = datetime.fromisoformat(date.replace('Z', '+00:00'))
                        results[path] = dt.strftime('%Y/%m/%d')
            return results

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def list_files_in_repo(self, session: aiohttp.ClientSession, path='') -> List[Dict]:

        # Create a TCPConnector with verify_ssl=False
        # connector = aiohttp.TCPConnector(ssl=False)

        # Create a new session with the connector if one isn't provided
        # if not session:
        # session = aiohttp.ClientSession(connector=connector)

        paths = []
        url = f'https://api.github.com/repos/{self.owner}/{self.repo}/contents/{path}'

        try:
            contents = await self.fetch_content(session, url)
            directories = []
            files_to_process = []

            for content in contents:
                if content['type'] == 'file':
                    if any(content['path'].endswith(ext) for ext in ('.pdf', '.zip', '.rar', '.7z', '.docx', '.doc',
                                                                     '.ipynb', '.pptx', '.apkg', '.mp4', '.csv',
                                                                     '.xlsx',
                                                                     'png', 'jpg', 'jpeg', 'gif', 'webp',
                                                                     '.md')):
                        files_to_process.append({
                            'path': content['path'],
                            'size': filesize.traditional(int(content['size']))
                        })
                elif content['type'] == 'dir':
                    directories.append(content['path'])

            # 批量获取文件的最后修改日期
            if files_to_process:
                file_dates = await self.get_commits_batch(session, [f['path'] for f in files_to_process])
                for file in files_to_process:
                    file['date'] = file_dates.get(file['path'], 'Unknown')
                paths.extend(files_to_process)

            # 处理目录
            if directories:
                tasks = [self.list_files_in_repo(session, directory) for directory in directories]
                results = await asyncio.gather(*tasks)
                for result in results:
                    paths.extend(result)

        except Exception as e:
            print(f"Error processing {url}: {str(e)}")

        return paths

    async def save_files_list(self):
        async with aiohttp.ClientSession() as session:
            start_time = time.time()
            paths = await self.list_files_in_repo(session)
            end_time = time.time()

            print(f"Retrieved {len(paths)} files in {end_time - start_time:.2f} seconds")

            if paths:
                result_content = await self.create_hugo_shortcode(paths)
                with open('result.txt', 'w', encoding="utf-8") as file:
                    file.write(result_content)

    async def create_hugo_shortcode(self, file_paths: List[Dict[str, str]]) -> str:
        result = f'{{{{< hoa-filetree/container driveURL="https://open.osa.moe/openauto/{self.repo}" >}}}}\n'
        organized_paths = self.organize_paths(file_paths)

        for directory, content in organized_paths.items():
            if isinstance(content, list) and is_human_readable_size(content[0]):
                prefix = f'https://gh.hoa.moe/github.com/{self.owner}/{self.repo}/raw/main'
                full_path = f'{prefix}/{directory}'

                name, suffix = directory.rsplit('.', 1)
                icon = match_suffix_icon(suffix)
                result += f'  {{{{< hoa-filetree/file name="{name}" type="{suffix}" size="{content[0]}" date="{content[1]}" icon="{icon}" url="{full_path}" >}}}}\n'
            else:
                folder_content = await self.generate_folder_content(directory, content)
                result += folder_content

        result += "{{< /hoa-filetree/container >}}\n"
        return result

    @staticmethod
    def organize_paths(file_paths: List[Dict[str, str]]) -> Dict:
        organized_paths = {}
        for path_dict in file_paths:
            current_dict = organized_paths
            path = path_dict['path']
            for component in path.split('/')[:-1]:
                current_dict = current_dict.setdefault(component, {})
            current_dict[path.split('/')[-1]] = [path_dict['size'], path_dict['date']]
        return organized_paths

    async def generate_folder_content(self, directory: str, content: Dict) -> str:
        result = f'  {{{{< hoa-filetree/folder name="{os.path.basename(directory)}" date="" state="closed" >}}}}\n'

        for name, value in content.items():
            if isinstance(value, list) and is_human_readable_size(value[0]):
                file_path = f'{directory}/{name}'
                encoded_path = urllib.parse.quote(file_path, safe='/')
                prefix = f'https://gh.hoa.moe/github.com/{self.owner}/{self.repo}/raw/main'
                full_path = f'{prefix}/{encoded_path}'
                name, suffix = name.rsplit('.', 1)
                icon = match_suffix_icon(suffix)
                result += f'    {{{{< hoa-filetree/file name="{name}" type="{suffix}" size="{value[0]}" date="{value[1]}" icon="{icon}" url="{full_path}" >}}}}\n'
            elif isinstance(value, dict):
                result += await self.generate_folder_content(f'{directory}/{name}', value)
        result += '  {{< /hoa-filetree/folder >}}\n'
        return result


def is_human_readable_size(size_str: str) -> bool:
    pattern = r'''(?x)
        ^
        (\d{1,10}|\d{1,10}\.\d{1,2})
        \s*
        (byte|bytes|[KMGTPE]B|B)
        $
    '''
    if not size_str:
        return False

    match = re.match(pattern, size_str, re.IGNORECASE)
    if not match:
        return False

    try:
        value = float(match.group(1))
        return value >= 0
    except ValueError:
        return False


def match_suffix_icon(suffix: str) -> str:
    image = ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'psd', 'tif', 'tiff']
    doc = ['doc', 'docx']
    icons = ['docx', 'pdf', 'image', 'ppt', 'txt', 'zip']

    if suffix in image:
        suffix = "image"
    if suffix in doc:
        suffix = "docx"
    if suffix not in icons:
        suffix = "file"

    return f"icons/{suffix}.png"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate download links of files from a GitHub repository.")
    parser.add_argument("owner", help="GitHub repository owner", default="HITSZ-OpenAuto")
    parser.add_argument("repo", help="GitHub repository name")
    parser.add_argument("token", help="GitHub token")
    
    args = parser.parse_args()

    client = GitHubAPIClient(args.owner, args.repo, args.token)
    asyncio.run(client.save_files_list())

