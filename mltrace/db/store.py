from mltrace.db.utils import _create_engine_wrapper, _initialize_db_tables, _drop_everything, _map_extension_to_enum
from mltrace.db import Component, ComponentRun, IOPointer, PointerTypeEnum, Tag
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker, joinedload

import logging
import typing


class Store(object):
    """Helper methods to interact with the db."""

    def __init__(self, uri: str, delete_first=False):
        """
        Creates the postgres database for the store. Raises exception if uri    
        isn't prefixed with postgresql://.

        Args:
            uri (str): URI string to connect to the SQLAlchemy database.
        """
        if not uri.startswith('postgresql://'):
            raise('Database URI must be prefixed with `postgresql://`')
        self.engine = _create_engine_wrapper(uri)

        # TODO(shreyashankar) remove this line
        if delete_first:
            _drop_everything(self.engine)

        # TODO(shreyashankar) check existing tables against expected tables
        _initialize_db_tables(self.engine)

        # Initialize session
        self.Session = sessionmaker(self.engine)
        self.session = self.Session()

    def __del__(self):
        """On destruction, close session."""
        self.session.close()

    def create_component(self, name: str, description: str, owner: str, tags: typing.List[str]):
        """Creates a component entity in the database if it does not already exist."""
        res = self._get_component(name)

        if res:
            logging.info(f'Component with name "{name}" already exists.')
            return

        # Add to the DB if it is not already there
        logging.info(
            f'Creating new Component with name "{name}", description "{description}", owner "{owner}", and tags "{tags}".')
        tags = [Tag(t) for t in tags]
        component = Component(
            name=name, description=description, owner=owner, tags=tags)
        self.session.add(component)
        self.session.commit()

    def _get_component(self, name: str) -> Component:
        """Retrieves component if exists."""
        return self.session.query(Component).filter(
            Component.name == name).first()

    def add_tags_to_component(self, component_name: str, tags: typing.List[str]):
        """Retreives existing component and adds tags."""
        component = self._get_component(component_name)

        if not component:
            raise(f'Component with name "{component_name}" not found.')

        tag_objects = [self.get_tag(t) for t in tags]
        component.add_tags(tag_objects)
        self.session.commit()

    def initialize_empty_component_run(self, component_name: str) -> ComponentRun:
        """Initializes an empty run for the specified component. Does not
        commit to the database."""
        component_run = ComponentRun(component_name=component_name)
        return component_run

    def get_tag(self, name=str) -> Tag:
        """Creates a tag around the name if it doesn't already exist."""
        res = self.session.query(Tag).filter(Tag.name == name).all()

        # Must create new Tag
        if len(res) == 0:
            logging.info(f'Creating new Tag with name "{name}".')
            tag = Tag(name)
            self.session.add(tag)
            self.session.commit()
            return tag

        # Return existing Tag
        return res[0]

    def get_io_pointer(self, name=str, pointer_type: PointerTypeEnum = None) -> IOPointer:
        """ Creates an io pointer around the specified path. 
        Retrieves existing io pointer if exists in DB, 
        otherwise creates a new one."""
        res = self.session.query(IOPointer).filter(IOPointer.name == name).all()

        # Must create new IOPointer
        if len(res) == 0:
            logging.info(f'Creating new IOPointer with name "{name}".')
            if pointer_type == None:
                pointer_type = _map_extension_to_enum(name)

            iop = IOPointer(name=name, pointer_type=pointer_type)
            self.session.add(iop)
            self.session.commit()
            return iop

        # Return existing object
        return res[0]

    def delete_component(self, component: Component):
        self.session.delete(component)
        logging.info(
            f'Successfully deleted Component with name "{component.name}".')

    def delete_component_run(self, component_run: ComponentRun):
        self.session.delete(component_run)
        logging.info(
            f'Successfully deleted ComponentRun with id "{component_run.id}" and name "{component_run.component_name}".')

    def delete_io_pointer(self, io_pointer: IOPointer):
        self.session.delete(io_pointer)
        logging.info(
            f'Successfully deleted IOPointer with name "{io_pointer.name}".')

    def create_output_ids(self, num_outputs=1) -> typing.List[str]:
        """Returns a list of num_outputs ids that don't already exist in the DB."""
        res = self.session.query(IOPointer).filter(
            IOPointer.pointer_type == PointerTypeEnum.ENDPOINT).all()

        start_index = 0 if len(res) == 0 else int(max(
            res, key=lambda x: int(x.name)).name) + 1

        return [f"{i}" for i in range(start_index, start_index + num_outputs)]

    def commit_component_run(self, component_run: ComponentRun):
        """Commits a fully initialized component run to the DB."""
        status_dict = component_run.check_completeness()
        if not status_dict['success']:
            raise(status_dict['msg'])

        # Commit to DB
        self.session.add(component_run)
        logging.info(
            f'Committing ComponentRun of type "{component_run.component_name}" to the database.')
        self.session.commit()

    def set_dependencies_from_inputs(self, component_run: ComponentRun):
        """ Gets IOPointers associated with component_run's inputs, checks
        against any ComponentRun's outputs, and if there are any matches, 
        sets the ComponentRun's dependency on the most recent match."""
        input_ids = [inp.name for inp in component_run.inputs]
        matches = self.session.query(ComponentRun, func.max(ComponentRun.start_timestamp).over(partition_by=ComponentRun.component_name))\
            .outerjoin(IOPointer, ComponentRun.outputs).filter(
            IOPointer.name.in_(input_ids)).all()
        matches = [m[0] for m in matches]

        # If there are no matches, return
        if len(matches) == 0:
            return

        # Get match with the max timestamp and set upstream
        component_run.set_upstream(matches)

    def _traverse(self, node: ComponentRun, depth: int, node_list: typing.List[ComponentRun]):
        # Add node to node_list as the step
        node_list.append((depth, node))

        # Base case
        if len(node.dependencies) == 0:
            return

        # Recurse on neighbors
        for neighbor in node.dependencies:
            self._traverse(neighbor, depth + 1, node_list)

    def trace(self, output_id: str):
        """Prints trace for an output id.
        Returns list of tuples (level, ComponentRun) where level is how
        many hops away the node is from the node that produced the output_id."""
        component_run_object = self.session.query(ComponentRun).options(
            joinedload('outputs')).filter(IOPointer.name == output_id).first()

        print(f'Printing trace for output {output_id}...')

        node_list = []
        self._traverse(component_run_object, 1, node_list)
        return node_list

    def trace_batch(self, output_ids: typing.List[str]):
        pass

    def get_history(self, component_name: str, limit: int = 10) -> typing.List[ComponentRun]:
        """Gets lineage for the component, or a history of all its runs."""
        history = self.session.query(ComponentRun).filter(ComponentRun.component_name == component_name).order_by(
            ComponentRun.start_timestamp.desc()).limit(limit).all()

        return history

    def get_components_with_owner(self, owner: str) -> typing.List[Component]:
        """ Returns a list of all the components associated with the specified
        order."""
        components = self.session.query(Component).filter(
            Component.owner == owner).options(joinedload('tags')).all()
        return components

    def get_components_with_tag(self, tag: str) -> typing.List[Component]:
        """Returns a list of all the components associated with that tag."""
        components = self.session.query(Component).join(
            Tag, Component.tags).filter(Tag.name == tag).all()
        return components