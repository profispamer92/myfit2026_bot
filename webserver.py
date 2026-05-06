"""
Lightweight aiohttp web server serving:
  GET  /                   → webapp/index.html
  GET  /api/day_plan       → full day plan JSON
  POST /api/mark_done      → mark supplement/task as done
"""
import json
import os
import logging
from aiohttp import web
from database import Database

logger = logging.getLogger(__name__)


def create_web_app(db: Database) -> web.Application:
    app = web.Application()

    # Serve webapp files
    async def serve_index(request):
        html_path = os.path.join(os.path.dirname(__file__), 'webapp', 'index.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')

    async def day_plan(request):
        try:
            user_id = int(request.rel_url.query.get('user_id', 0))
            if not user_id:
                return web.json_response({'error': 'no user_id'}, status=400)
            plan = db.get_full_day_plan(user_id)
            return web.json_response(plan)
        except Exception as e:
            logger.error(f"day_plan error: {e}")
            return web.json_response({'error': str(e)}, status=500)

    async def mark_done(request):
        try:
            body = await request.json()
            user_id = int(body.get('user_id', 0))
            item_type = body.get('type')
            item_id = int(body.get('id', 0))

            if not user_id or not item_type or not item_id:
                return web.json_response({'error': 'missing fields'}, status=400)

            if item_type == 'supplement':
                db.log_supplement_taken(user_id, item_id)
            elif item_type == 'task':
                db.log_task_done(user_id, item_id)
            else:
                return web.json_response({'error': 'unknown type'}, status=400)

            return web.json_response({'ok': True})
        except Exception as e:
            logger.error(f"mark_done error: {e}")
            return web.json_response({'error': str(e)}, status=500)

    app.router.add_get('/', serve_index)
    app.router.add_get('/api/day_plan', day_plan)
    app.router.add_post('/api/mark_done', mark_done)

    return app


async def start_web_server(db: Database, port: int = 8080):
    app = create_web_app(db)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on port {port}")
    return runner
