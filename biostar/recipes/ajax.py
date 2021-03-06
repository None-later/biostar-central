from functools import wraps, partial
import logging

from django.contrib import messages
import toml
from ratelimit.decorators import ratelimit

from django.shortcuts import reverse
from django.template import loader
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django.template import Template, Context
from django.http import JsonResponse
from django.template import loader
from django.conf import settings
from biostar.accounts.models import User
from biostar.recipes.const import *
from biostar.recipes.models import Job, Analysis, Data, Snippet, SnippetType, Project, MAX_TEXT_LEN, Access
from biostar.recipes.forms import RecipeInterface
from biostar.recipes import auth
from django.shortcuts import render, redirect, reverse

logger = logging.getLogger("engine")

JOB_COLORS = {Job.SPOOLED: "spooled",
              Job.ERROR: "errored", Job.QUEUED: "queued",
              Job.RUNNING: "running", Job.COMPLETED: "completed"
              }


def ajax_msg(msg, status, **kwargs):
    payload = dict(status=status, msg=msg)
    payload.update(kwargs)
    return JsonResponse(payload)


ajax_success = partial(ajax_msg, status='success')
ajax_error = partial(ajax_msg, status='error')

MIN_TITLE_CHARS = 10
MAX_TITLE_CHARS = 180
MAX_SNIPPET = 180
MAX_HELP = 180
MAX_SNIPPETS_CATEGORIES = 10
MAX_SNIPPETS_PER_CATEGORY = 30

MAX_TAGS = 5


class ajax_error_wrapper:
    """
    Used as decorator to trap/display  errors in the ajax calls
    """

    def __init__(self, method, login_required=True):
        self.method = method
        self.login_required = login_required

    def __call__(self, func, *args, **kwargs):

        @wraps(func)
        def _ajax_view(request, *args, **kwargs):

            if request.method != self.method:
                return ajax_error(f'{self.method} method must be used.')
            if request.user.is_anonymous and self.login_required:
                return ajax_error('You must be logged in.')
            return func(request, *args, **kwargs)

        return _ajax_view


from .forms import RecipeForm


@ajax_error_wrapper(method="POST", login_required=True)
def ajax_edit(request, id):
    """
    Edit recipes. Uses recipe primary key as access!
    """

    # The user performing the request.
    user = request.user

    # The recipe that needs to be edited.
    recipe = Analysis.objects.filter(id=id).first()

    if not auth.writeable_recipe(source=recipe, user=user):
        message = str("Recipe is not writable by current user")
        return ajax_error(msg=message)

    # The project of for the recipe.
    project = recipe.project

    form = RecipeForm(data=request.POST, instance=recipe, files=request.FILES, user=user,
                      project=project)

    if form.is_valid():
        form.save()
        return ajax_success(msg=f"Recipe saved.")
    else:
        message = str(form.errors)
        return ajax_error(msg=message)


def check_job(request, uid):
    job = Job.objects.filter(uid=uid).first()

    check_back = 'check_back' if job.state in [Job.SPOOLED, Job.RUNNING] else ''
    stdout_path = os.path.join(job.path, settings.JOB_STDOUT)
    stderr_path = os.path.join(job.path, settings.JOB_STDERR)

    try:
        state_changed = int(request.GET.get('state')) != job.state
    except Exception as exc:
        logger.error(f'Error checking job:{exc}')
        state_changed = False

    if os.path.exists(stdout_path) and os.path.exists(stderr_path):
        stdout = open(stdout_path, 'r').read()
        stderr = open(stderr_path, 'r').read()
        Job.objects.filter(uid=job.uid).update(stderr_log=stderr, stdout_log=stdout)
    else:
        stdout = stderr = None

    # Get an the loading image icon
    redir = job.url() if job.is_finished() else ""
    context = dict(job=job)
    tmpl = loader.get_template('widgets/loading_img.html')
    image_tmpl = tmpl.render(context=context)

    context = dict(job=job, check_back=check_back)
    tmpl = loader.get_template('widgets/job_elapsed.html')
    template = tmpl.render(context=context)

    return ajax_success(msg='success', redir=redir,
                        html=template, state=job.get_state_display(), is_running=job.is_running(),
                        stdout=stdout, stderr=stderr, job_color=auth.job_color(job),
                        state_changed=state_changed, img_tmpl=image_tmpl)


def check_size(fobj, maxsize=0.3):
    # maxsize in megabytes!

    try:
        if fobj and fobj.size > maxsize * 1024 * 1024.0:
            curr_size = fobj.size / 1024 / 1024.0
            msg = f"File too large: {curr_size:0.1f}MB should be < {maxsize:0.1f}MB"
            return msg, False
    except Exception as exc:
        return f"File size validation error: {exc}", False

    return "Valid", True


@ajax_error_wrapper(method="POST", login_required=False)
def preview_template(request):
    source_template = request.POST.get('template', '# Code goes here')
    source_json = request.POST.get('json_text', '{}')
    project_uid = request.POST.get('project_uid')
    project = Project.objects.filter(uid=project_uid).first()
    try:
        source_json = toml.loads(source_json)
        # Fill json information by name for the preview.
        source_json = auth.fill_data_by_name(project=project, json_data=source_json)
        # Fill in the script with json data.
        context = Context(source_json)
        script_template = Template(source_template)
        script = script_template.render(context)
    except Exception as exc:
        return ajax_error(f"Error rendering code: {exc}")

    # Load the html containing the script
    return ajax_success(script=script, msg="Rendered script")


@ajax_error_wrapper(method="POST", login_required=False)
def preview_json(request):
    # Get the recipe
    recipe_name = request.POST.get('name')
    project_uid = request.POST.get('project_uid')

    json_text = request.POST.get('json_text', '')
    try:
        json_data = toml.loads(json_text)
    except Exception as exc:
        return ajax_error(msg=f"{exc}")

    project = Project.objects.filter(uid=project_uid).first()
    if not project:
        return ajax_error(msg=f"Project does not exist")

    # Render the recipe interface
    interface = RecipeInterface(request=request, json_data=json_data, project=project,
                                initial=dict(name=recipe_name), add_captcha=False)

    tmpl = loader.get_template('widgets/recipe_form.html')
    context = dict(form=interface, focus=True)
    template = tmpl.render(context=context)

    return ajax_success(msg="Recipe json", html=template)


def builder(display, **initial):

    data = dict(label="Insert Field Label", display=display)
    defaults = dict()
    if display == INTEGER or display == FLOAT:
        defaults = dict(range=[0, 100], value=0, help="Enter a number.")

    elif display == DROPDOWN or display == RADIO:
        defaults = dict(choices=[("1", 'Option 1'), ("2", 'Option 2')],
                        value=2, help="Enter a number.")
    data.update(defaults)
    data.update(initial)

    return data


def display_data(display_type):

    # Special case where "display" key is not added
    if display_type == 'data':
        return dict(label='Data Field Label',
                    source='PROJECT',
                    type='DATA',
                    help='Pick data from this project to analyze.')

    integer = builder(display=INTEGER)
    floating = builder(display=FLOAT)
    dropdown = builder(display=DROPDOWN)
    radio = builder(display=RADIO)
    upload = builder(display=UPLOAD, **dict(help="Upload a file to analyze"))
    checkbox = builder(display=CHECKBOX, **dict(value=True, help='Checkbox.'))
    textbox = builder(display=TEXTBOX, **dict(value='Example text', help='Enter text.'))

    data_map = dict(integer=integer, textbox=textbox, float=floating, checkbox=checkbox,
                    dropdown=dropdown, upload=upload, radio=radio)

    data = data_map.get(display_type, dict())

    return data


@ajax_error_wrapper(method="POST", login_required=False)
def add_to_interface(request):
    # Returns a recipe interface field json

    # Get the display type to add
    display_type = request.POST.get('display_types', '')
    json_text = request.POST.get('json_text', '')
    display_dict = display_data(display_type=display_type)
    try:
        json_data = toml.loads(json_text)
    except Exception as exc:
        return ajax_error(msg=f"{exc}")

    prefix = "Parameter"
    field_name = prefix
    count = 0
    # Check if the field name exists
    while field_name in json_data:
        field_name = prefix + f'{count}'
        count += 1

    new_field = {field_name: display_dict}
    json_data.update(new_field)
    new_json = toml.dumps(json_data)

    tmpl = loader.get_template('widgets/json_field.html')
    context = dict(json_text=new_json, focus=True)
    json_field = tmpl.render(context=context)

    return ajax_success(html=json_field, json_text=new_json, msg="Rendered json")


@ajax_error_wrapper(method="POST")
def file_copy(request):
    """
    Add file into clipboard.
    """
    path = request.POST.get('path')
    fullpath = os.path.abspath(os.path.join(settings.IMPORT_ROOT_DIR, path))
    if not os.path.exists(fullpath):
        return ajax_error(msg="File path does not exist.")

    copied = auth.copy_file(request=request, fullpath=fullpath)

    return ajax_success(msg=f"{len(copied)} files copied.")


@ajax_error_wrapper(method="POST", login_required=True)
def toggle_delete(request):
    """
    Delete an object.
    """

    type_map = dict(job=Job, data=Data, recipe=Analysis)
    uid = request.POST.get('uid', "")
    obj_type = request.POST.get('type', '')

    obj_model = type_map.get(obj_type)

    if not obj_model:
        return ajax_error(f"Invalid data type:{obj_type}")

    obj = obj_model.objects.filter(uid=uid).first()

    if not obj:
        return ajax_error("Object does not exists.")

    access = auth.is_writable(user=request.user, project=obj.project)

    # Toggle the delete state if the user has write access
    if access:
        auth.delete_object(obj=obj, request=request)
        # Re-set project counts
        obj.project.set_counts(save=True)
        counts = obj_model.objects.filter(project=obj.project, deleted=False).count()
        return ajax_success(msg='Toggled delete', counts=counts)

    return ajax_error("Invalid action")


@ratelimit(key='ip', rate='10/m')
@ajax_error_wrapper(method="POST", login_required=True)
def manage_access(request):
    access_map = dict(none=Access.NO_ACCESS, read=Access.READ_ACCESS,
                      write=Access.WRITE_ACCESS, share=Access.SHARE_ACCESS)

    # Get the current user, project and access
    user_id = request.POST.get('user_id', '')
    project_uid = request.POST.get('project_uid', '')
    access_str = request.POST.get('access', '')

    user = User.objects.filter(id=user_id).first()
    new_access = access_map.get(access_str)
    project = Project.objects.filter(uid=project_uid).first()
    is_writable = auth.is_writable(user=request.user, project=project)

    # Validate submitted values.
    if not project:
        return ajax_error("Project does not exist.")
    if not user:
        return ajax_error("User does not exist.")
    if not new_access:
        return ajax_error(f"Invalid access option: {access_str}")
    if user == request.user:
        return ajax_error("Can not change your own access")
    if user == project.owner:
        return ajax_error("Can not change the project owner's access")
    if not is_writable:
        return ajax_error("You need write access to manage access.")

    # Check current user access.
    access = Access.objects.filter(user=user, project=project).first()

    # Update existing access
    if access:
        Access.objects.filter(id=access.id).update(access=new_access)
    # Create a new access object
    else:
        Access.objects.create(user=user, project=project, access=new_access)

    no_access = new_access == Access.NO_ACCESS

    return ajax_success("Changed access.", no_access=no_access)


@ajax_error_wrapper(method="POST", login_required=True)
def copy_object(request):
    """
    Add object uid or file path to sessions clipboard.
    """

    object_uid = request.POST.get('uid', '')
    project_uid = request.POST.get('project_uid', '')
    clipboard = request.POST.get('clipboard')

    project = Project.objects.filter(uid=project_uid).first()
    if not project:
        return ajax_error("Project does not exist.")

    is_readable = auth.is_readable(user=request.user, project=project)

    if not is_readable:
        return ajax_error('You do not have access to copy this object.')

    # Return current clipboard contents
    copied_uids = auth.copy_uid(request=request, uid=object_uid, board=clipboard)

    return ajax_success(f"Copied. Clipboard contains :{len(copied_uids)} objects.")


def add_variables(request):
    # Get the most recent template and json.
    json_text = request.POST.get('json_text', '')
    template = request.POST.get('template', '')

    json_data = toml.loads(json_text)

    # Create a set with all template variables
    all_vars = {"{{ " + f"{v}.value" + "}}" for v in json_data.keys()}

    all_vars = '\n'.join(all_vars)

    # Insert variables at the beginning of the template
    new_template = template + '\n' + all_vars

    tmpl = loader.get_template('widgets/template_field.html')
    context = dict(template=new_template, scroll_to_bottom=False, focus=True)
    template_field = tmpl.render(context=context)

    return ajax_success(msg="Added variables to template", html=template_field, code=new_template)


def field_render(request):
    """
    Renders and returns an HTML field based on the incoming toml data.
    """
    from .forms import RecipeInterface
    from django.http import HttpResponse
    import toml

    demo = """
    [demo]
    label = "Demo"
    display = "INTEGER"
    value = 10000000
    """
    text = request.POST.get("toml", demo)

    try:
        data = toml.loads(text)
    except Exception as exc:
        return HttpResponse(exc)

    form = RecipeInterface(request, json_data=data)

    context = dict(form=form)

    return render(request, "parts/form_field_render.html", context=context)
