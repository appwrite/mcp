from __future__ import annotations
import asyncio
import os
import argparse
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.shared.exceptions import McpError
from dotenv import load_dotenv
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.services.users import Users
from appwrite.services.teams import Teams
from appwrite.services.storage import Storage
from appwrite.services.functions import Functions
from appwrite.services.locale import Locale
from appwrite.services.avatars import Avatars
from appwrite.services.messaging import Messaging
from appwrite.services.sites import Sites
from appwrite.exception import AppwriteException
from .tool_manager import ToolManager
from .service import Service

def parse_args():
    parser = argparse.ArgumentParser(description='Appwrite MCP Server')
    parser.add_argument('--databases', action='store_true', help='Enable Databases service')
    parser.add_argument('--users', action='store_true', help='Enable Users service')
    parser.add_argument('--teams', action='store_true', help='Enable Teams service')
    parser.add_argument('--storage', action='store_true', help='Enable Storage service')
    parser.add_argument('--functions', action='store_true', help='Enable Functions service')
    parser.add_argument('--messaging', action='store_true', help='Enable Messaging service')
    parser.add_argument('--locale', action='store_true', help='Enable Locale service')
    parser.add_argument('--avatars', action='store_true', help='Enable Avatars service')
    parser.add_argument('--sites', action='store_true', help='Enable Sites service')
    parser.add_argument('--all', action='store_true', help='Enable all services')
    return parser.parse_args()

# Load environment variables from .env file
load_dotenv()

# Get environment variables
project_id = os.getenv('APPWRITE_PROJECT_ID')
api_key = os.getenv('APPWRITE_API_KEY')
endpoint = os.getenv('APPWRITE_ENDPOINT', 'https://cloud.appwrite.io/v1')

if not project_id or not api_key:
    raise ValueError("APPWRITE_PROJECT_ID and APPWRITE_API_KEY must be set in environment variables")

# Initialize Appwrite client
client = Client()
client.set_endpoint(endpoint)
client.set_project(project_id)
client.set_key(api_key)

# Initialize tools manager
tools_manager = ToolManager()

def register_services(args):
    # If --all is specified, enable all services
    if args.all:
        args.databases = args.users = args.teams = args.storage = True
        args.functions = args.messaging = args.locale = args.avatars = True
        args.sites = True

    # Register services based on CLI arguments
    if args.databases:
        tools_manager.register_service(Service(Databases(client), "databases"))
    if args.users:
        tools_manager.register_service(Service(Users(client), "users"))
    if args.teams:
        tools_manager.register_service(Service(Teams(client), "teams"))
    if args.storage:
        tools_manager.register_service(Service(Storage(client), "storage"))
    if args.functions:
        tools_manager.register_service(Service(Functions(client), "functions"))
    if args.messaging:
        tools_manager.register_service(Service(Messaging(client), "messaging"))
    if args.locale:
        tools_manager.register_service(Service(Locale(client), "locale"))
    if args.avatars:
        tools_manager.register_service(Service(Avatars(client), "avatars"))
    if args.sites:
        tools_manager.register_service(Service(Sites(client), "sites"))

    # If no services were specified, enable databases by default
    if not any([args.databases, args.users, args.teams, args.storage,
                args.functions, args.messaging, args.locale, args.avatars]):
        tools_manager.register_service(Service(Databases(client), "databases"))

async def serve() -> Server:
    server = Server("Appwrite MCP Server")
    
    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return tools_manager.get_all_tools()

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        
        try:
            tool_info = tools_manager.get_tool(name)
            if not tool_info:
                raise McpError(f"Tool {name} not found")
            
            bound_method = tool_info["function"]
            result = bound_method(**(arguments or {}))
            if hasattr(result, 'to_dict'):
                result_dict = result.to_dict()
                return [types.TextContent(type="text", text=str(result_dict))]
            return [types.TextContent(type="text", text=str(result))]
        except AppwriteException as e:
            return [types.TextContent(type="text", text=f"Appwrite Error: {str(e)}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {str(e)}")]

    return server

async def _run():
    args = parse_args()
    register_services(args)
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        server = await serve()
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="appwrite",
                server_version="0.2.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(_run())