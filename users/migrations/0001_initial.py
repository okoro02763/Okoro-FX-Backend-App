import django.contrib.auth.models
import django.contrib.auth.validators
import django.utils.timezone
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class CreateModelIfMissing(Operation):
    """Create a model's table only when it does not already exist.

    The live database already had ``auth_user`` -- created by Django's built-in
    ``auth`` migrations with an integer primary key -- when this dedicated
    ``users`` app was introduced. A plain ``CreateModel`` therefore failed with
    ``relation "auth_user" already exists``, so the migration could never be
    recorded and every signup INSERT failed against the UUID model. Fresh
    databases still get a real table.

    This keeps the historical ``CreateModel`` as the state operation (so the
    autodetector sees exactly the model declared in ``users/models.py``) while
    making the database side tolerant of the pre-existing table.
    """

    reversible = True
    reduces_to_sql = False
    atomic = False

    def __init__(self, create_model):
        self.create_model = create_model

    def state_forwards(self, app_label, state):
        self.create_model.state_forwards(app_label, state)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, 'User')
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        existing_tables = schema_editor.connection.introspection.table_names()
        if model._meta.db_table not in existing_tables:
            schema_editor.create_model(model)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Never drop the user table on reverse: it may hold real accounts that
        # pre-date this migration.
        pass

    def deconstruct(self):
        return (self.__class__.__qualname__, [self.create_model], {})

    def describe(self):
        return 'Create the user table only if it is missing'

    @property
    def migration_name_fragment(self):
        return 'create_user_table_if_missing'


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        CreateModelIfMissing(
            migrations.CreateModel(
                name='User',
                fields=[
                    ('password', models.CharField(max_length=128, verbose_name='password')),
                    ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                    ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                    ('username', models.CharField(error_messages={'unique': 'A user with that username already exists.'}, help_text='Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.', max_length=150, unique=True, validators=[django.contrib.auth.validators.UnicodeUsernameValidator()], verbose_name='username')),
                    ('first_name', models.CharField(blank=True, max_length=150, verbose_name='first name')),
                    ('last_name', models.CharField(blank=True, max_length=150, verbose_name='last name')),
                    ('email', models.EmailField(blank=True, max_length=254, verbose_name='email address')),
                    ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
                    ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.', verbose_name='active')),
                    ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
                    ('id', models.AutoField(primary_key=True, serialize=False)),
                    ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
                    ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
                ],
                options={
                    'db_table': 'auth_user',
                },
                managers=[
                    ('objects', django.contrib.auth.models.UserManager()),
                ],
            )
        ),
    ]
