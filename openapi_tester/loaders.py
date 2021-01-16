import difflib
import json
import logging
from json import dumps, loads
from typing import Callable, List, Optional
from urllib.parse import ParseResult

import yaml
from django.core.exceptions import ImproperlyConfigured
from django.urls import Resolver404, resolve
from openapi_spec_validator import openapi_v2_spec_validator, openapi_v3_spec_validator
from prance.util.resolver import RefResolver
from prance.util.url import ResolutionError
from rest_framework.schemas.generators import EndpointEnumerator

from openapi_tester.exceptions import OpenAPISchemaError
from openapi_tester.route import Route

logger = logging.getLogger('openapi_tester')


def handle_recursion_limit(schema: dict) -> Callable:
    """
    We are using a currying pattern to pass schema into the scope of the handler.
    """

    def handler(iteration: int, parse_result: ParseResult, recursions: tuple):
        try:
            fragment = parse_result.fragment
            keys = [key for key in fragment.split('/') if key]
            definition = schema
            for key in keys:
                definition = definition[key]
            return remove_recursive_ref(definition, fragment)
        except KeyError:
            return {}

    return handler


def remove_recursive_ref(schema: dict, fragment: str) -> dict:
    """
    Iterates over a dictionary to look for pesky recursive $refs using the fragment identifier.
    """
    for key, value in schema.items():
        if isinstance(value, dict):
            if '$ref' in value.keys() and fragment in value['$ref']:
                # TODO: use this value in the testing - to ignore some parts of the specs
                schema[key] = {'x-recursive-ref-replaced': True}
            else:
                schema[key] = remove_recursive_ref(schema[key], fragment)
    return schema


class BaseSchemaLoader:
    """
    Base class for OpenAPI schema loading classes.

    Contains a template of methods that are required from a loader class, and a range of helper methods for interacting
    with an OpenAPI schema.
    """

    base_path = '/'

    def __init__(self):
        super().__init__()
        self.schema: Optional[dict] = None

    def load_schema(self) -> dict:
        """
        Put logic required to load a schema and return it here.
        """
        raise NotImplementedError('The `load_schema` method has to be overwritten.')

    def get_schema(self) -> dict:
        """
        Returns OpenAPI schema.
        """
        if self.schema is None:
            self.set_schema(self.load_schema())
        return self.schema  # type: ignore

    def dereference_schema(self, schema: dict) -> dict:
        try:
            url = schema['basePath'] if 'basePath' in schema else self.base_path
            resolver = RefResolver(
                schema,
                recursion_limit_handler=handle_recursion_limit(schema),
                url=url,
            )
            resolver.resolve_references()
            return resolver.specs
        except ResolutionError as e:
            raise OpenAPISchemaError('infinite recursion error') from e

    @staticmethod
    def validate_schema(schema: dict):
        if 'openapi' in schema:
            validator = openapi_v3_spec_validator
        else:
            validator = openapi_v2_spec_validator
        validator.validate(schema)

    def set_schema(self, schema: dict) -> None:
        """
        Sets self.schema and self.original_schema.
        """
        dereferenced_schema = self.dereference_schema(schema)
        self.validate_schema(dereferenced_schema)
        self.schema = self.dereference_schema(dereferenced_schema)

    def get_route(self, route: str) -> Route:
        """
        Returns the appropriate endpoint route.

        This method was primarily implemented because drf-yasg has its own route style, and so this method
        lets loader classes overwrite and add custom route conversion logic if required.
        """
        return Route(*self.resolve_path(route))

    @staticmethod
    def get_endpoint_paths() -> List[str]:
        """
        Returns a list of endpoint paths.
        """
        return list({endpoint[0] for endpoint in EndpointEnumerator().get_api_endpoints()})

    def resolve_path(self, endpoint_path: str) -> tuple:
        """
        Resolves a Django path.
        """
        try:
            logger.debug('Resolving path.')
            if '?' in endpoint_path:
                endpoint_path = endpoint_path.split('?')[0]
            if endpoint_path == '' or endpoint_path[0] != '/':
                logger.debug('Adding leading `/` to provided path')
                endpoint_path = '/' + endpoint_path
            if len(endpoint_path) > 2 and endpoint_path[-1] == '/':
                endpoint_path = endpoint_path[:-1]
            try:
                resolved_route = resolve(endpoint_path)
                logger.debug('Resolved %s successfully', endpoint_path)
            except Resolver404:
                resolved_route = resolve(endpoint_path + '/')
                endpoint_path += '/'
            kwarg = resolved_route.kwargs
            for key, value in kwarg.items():
                # Replacing kwarg values back into the string seems to be the simplest way of bypassing complex regex
                # handling. However, its important not to freely use the .replace() function, as a {value} of `1` would
                # also cause the `1` in api/v1/ to be replaced
                var_index = endpoint_path.rfind(str(value))
                endpoint_path = endpoint_path[:var_index] + f'{{{key}}}' + endpoint_path[var_index + len(str(value)) :]
            return endpoint_path, resolved_route

        except Resolver404:
            logger.warning('URL `%s` did not resolve successfully', endpoint_path)
            paths = self.get_endpoint_paths()
            closest_matches = ''.join(f'\n- {i}' for i in difflib.get_close_matches(endpoint_path, paths))
            if closest_matches:
                raise ValueError(
                    f'Could not resolve path `{endpoint_path}`.\n\nDid you mean one of these?{closest_matches}\n\n'
                    f'If your path contains path parameters (e.g., `/api/<version>/...`), make sure to pass a '
                    f'value, and not the parameter pattern.'
                )
            raise ValueError(f'Could not resolve path `{endpoint_path}`')


class DrfYasgSchemaLoader(BaseSchemaLoader):
    """
    Loads OpenAPI schema generated by drf_yasg.
    """

    def __init__(self) -> None:
        super().__init__()
        from drf_yasg.generators import OpenAPISchemaGenerator
        from drf_yasg.openapi import Info

        logger.debug('Initialized drf-yasg loader schema')
        self.schema_generator = OpenAPISchemaGenerator(info=Info(title='', default_version=''))

    def load_schema(self) -> dict:
        """
        Loads generated schema from drf-yasg and returns it as a dict.
        """
        odict_schema = self.schema_generator.get_schema(None, True)
        schema = loads(dumps(odict_schema.as_odict()))
        logger.debug('Successfully loaded schema')
        return schema

    def get_path_prefix(self) -> str:
        """
        Returns the drf_yasg specified path prefix.

        Drf_yasg `cleans` schema paths by finding recurring path patterns,
        and cutting them out of the generated openapi schema.
        For example, `/api/v1/example` might then just become `/example`
        """

        return self.schema_generator.determine_path_prefix(self.get_endpoint_paths())

    def get_route(self, route: str) -> Route:
        """
        Returns a url that matches the urls found in a drf_yasg-generated schema.

        :param route: Django resolved route
        """

        de_parameterized_path, resolved_path = self.resolve_path(route)
        path_prefix = self.get_path_prefix()  # typically might be 'api/' or 'api/v1/'
        if path_prefix == '/':
            path_prefix = ''
        logger.debug('Path prefix: %s', path_prefix)
        return Route(de_parameterized_path=de_parameterized_path[len(path_prefix) :], resolved_path=resolved_path)


class DrfSpectacularSchemaLoader(BaseSchemaLoader):
    """
    Loads OpenAPI schema generated by drf_spectacular.
    """

    def __init__(self) -> None:
        super().__init__()
        from drf_spectacular.generators import SchemaGenerator

        self.schema_generator = SchemaGenerator()
        logger.debug('Initialized drf-spectacular loader schema')

    def load_schema(self) -> dict:
        """
        Loads generated schema from drf_spectacular and returns it as a dict.
        """
        return loads(dumps(self.schema_generator.get_schema(None, True)))

    def get_path_prefix(self) -> str:
        """
        Returns the drf_spectacular specified path prefix.
        """
        from drf_spectacular.settings import spectacular_settings

        return spectacular_settings.SCHEMA_PATH_PREFIX

    def get_route(self, route: str) -> Route:
        """
        Returns a url that matches the urls found in a drf_spectacular-generated schema.

        :param route: Django resolved route
        """

        de_parameterized_path, resolved_path = self.resolve_path(route)
        path_prefix = self.get_path_prefix()  # typically might be 'api/' or 'api/v1/'
        if path_prefix == '/':
            path_prefix = ''
        logger.debug('Path prefix: %s', path_prefix)
        return Route(de_parameterized_path=de_parameterized_path[len(path_prefix) :], resolved_path=resolved_path)


class StaticSchemaLoader(BaseSchemaLoader):
    """
    Loads OpenAPI schema from a static file.
    """

    is_static_loader = True

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        logger.debug('Initialized static loader schema')

    def load_schema(self) -> dict:
        """
        Loads a static OpenAPI schema from file, and parses it to a python dict.

        :return: Schema contents as a dict
        :raises: ImproperlyConfigured
        """
        try:
            logger.debug('Fetching static schema from %s', self.path)
            with open(self.path) as f:
                content = f.read()
                if '.json' in self.path:
                    schema = json.loads(content)
                else:
                    schema = yaml.load(content, Loader=yaml.FullLoader)
            logger.debug('Successfully loaded schema')
            return schema
        except Exception as e:
            raise ImproperlyConfigured(
                'Unable to read the schema file. Please make sure the path setting is correct.'
            ) from e
