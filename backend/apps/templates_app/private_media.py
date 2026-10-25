"""Private uploaded assets are downloadable only with the asset's model permission."""
from pathlib import PurePosixPath

from django.http import FileResponse, JsonResponse
from apps.core.http import api_login_required, method_not_allowed
from .models import DocumentTemplate, Letterhead, TemplateBlock


@api_login_required
def private_media(request, name):
    if request.method != 'GET':
        return method_not_allowed(['GET'])
    for model, field in ((DocumentTemplate, 'style_template'), (Letterhead, 'docx'),
                         (TemplateBlock, 'docx_template')):
        if not request.user.has_perm(f'templates_app.view_{model._meta.model_name}'):
            continue
        record = model.objects.filter(**{field: name}).first()
        if record is None:
            continue
        try:
            asset = getattr(record, field).open('rb')
        except (FileNotFoundError, OSError):
            break
        response = FileResponse(asset, as_attachment=True, filename=PurePosixPath(name).name,
                                content_type='application/octet-stream')
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Security-Policy'] = "sandbox; default-src 'none'"
        response['Cache-Control'] = 'private, no-store'
        return response
    return JsonResponse({'error': 'Asset not found or unavailable'}, status=404)
